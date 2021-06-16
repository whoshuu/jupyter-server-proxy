"""
Authenticated HTTP proxy for Jupyter Notebooks

Some original inspiration from https://github.com/senko/tornado-proxy
"""

import inspect
import socket
import os
from urllib.parse import urlunparse, urlparse, quote
import aiohttp
from asyncio import Lock

from tornado import gen, web, httpclient, httputil, process, websocket, ioloop, version_info

from jupyter_server.utils import ensure_async, url_path_join
from jupyter_server.base.handlers import JupyterHandler, utcnow

from .utils import call_with_asked_args
from .websocket import WebSocketHandlerMixin, pingable_ws_connect
from simpervisor import SupervisedProcess


def file_log(message, extra="global"):
    with open("/tmp/debug.log", "a") as f:
        f.write("JSP/handlers.py " + extra + " " + message + "\n")


class AddSlashHandler(JupyterHandler):
    """Add trailing slash to URLs that need them."""
    @web.authenticated
    def get(self, *args):
        file_log(f"get {self.request.path}", self.__class__.__name__)
        src = urlparse(self.request.uri)
        dest = src._replace(path=src.path + '/')
        self.redirect(urlunparse(dest))

class ProxyHandler(WebSocketHandlerMixin, JupyterHandler):
    """
    A tornado request handler that proxies HTTP and websockets from
    a given host/port combination. This class is not meant to be
    used directly as a means of overriding CORS. This presents significant
    security risks, and could allow arbitrary remote code access. Instead, it is
    meant to be subclassed and used for proxying URLs from trusted sources.

    Subclasses should implement open, http_get, post, put, delete, head, patch,
    and options.
    """
    def __init__(self, *args, **kwargs):
        file_log(f"__init__ {self.request.path}", self.__class__.__name__)
        self.proxy_base = ''
        self.absolute_url = kwargs.pop('absolute_url', False)
        self.host_allowlist = kwargs.pop('host_allowlist', ['localhost', '127.0.0.1'])
        self.subprotocols = None
        super().__init__(*args, **kwargs)

    # Support all the methods that tornado does by default except for GET which
    # is passed to WebSocketHandlerMixin and then to WebSocketHandler.

    async def open(self, port, proxied_path):
        file_log(f"open {self.request.path}", self.__class__.__name__)
        raise NotImplementedError('Subclasses of ProxyHandler should implement open')

    async def http_get(self, host, port, proxy_path=''):
        file_log(f"http_get {self.request.path}", self.__class__.__name__)
        '''Our non-websocket GET.'''
        raise NotImplementedError('Subclasses of ProxyHandler should implement http_get')

    def post(self, host, port, proxy_path=''):
        file_log(f"post {self.request.path}", self.__class__.__name__)
        raise NotImplementedError('Subclasses of ProxyHandler should implement this post')

    def put(self, port, proxy_path=''):
        file_log(f"put {self.request.path}", self.__class__.__name__)
        raise NotImplementedError('Subclasses of ProxyHandler should implement this put')

    def delete(self, host, port, proxy_path=''):
        file_log(f"delete {self.request.path}", self.__class__.__name__)
        raise NotImplementedError('Subclasses of ProxyHandler should implement delete')

    def head(self, host, port, proxy_path=''):
        file_log(f"head {self.request.path}", self.__class__.__name__)
        raise NotImplementedError('Subclasses of ProxyHandler should implement head')

    def patch(self, host, port, proxy_path=''):
        file_log(f"patch {self.request.path}", self.__class__.__name__)
        raise NotImplementedError('Subclasses of ProxyHandler should implement patch')

    def options(self, host, port, proxy_path=''):
        file_log(f"options {self.request.path}", self.__class__.__name__)
        raise NotImplementedError('Subclasses of ProxyHandler should implement options')

    def on_message(self, message):
        file_log(f"on_message {self.request.path}", self.__class__.__name__)
        """
        Called when we receive a message from our client.

        We proxy it to the backend.
        """
        self._record_activity()
        if hasattr(self, 'ws'):
            self.ws.write_message(message, binary=isinstance(message, bytes))

    def on_ping(self, data):
        file_log(f"on_ping {self.request.path}", self.__class__.__name__)
        """
        Called when the client pings our websocket connection.

        We proxy it to the backend.
        """
        self.log.debug('jupyter_server_proxy: on_ping: {}'.format(data))
        self._record_activity()
        if hasattr(self, 'ws'):
            self.ws.protocol.write_ping(data)

    def on_pong(self, data):
        file_log(f"on_pong {self.request.path}", self.__class__.__name__)
        """
        Called when we receive a ping back.
        """
        self.log.debug('jupyter_server_proxy: on_pong: {}'.format(data))

    def on_close(self):
        file_log(f"on_close {self.request.path}", self.__class__.__name__)
        """
        Called when the client closes our websocket connection.

        We close our connection to the backend too.
        """
        if hasattr(self, 'ws'):
            self.ws.close()

    def _record_activity(self):
        file_log(f"_record_activity {self.request.path}", self.__class__.__name__)
        """Record proxied activity as API activity

        avoids proxied traffic being ignored by the notebook's
        internal idle-shutdown mechanism
        """
        self.settings['api_last_activity'] = utcnow()

    def _get_context_path(self, host, port):
        file_log(f"_get_context_path {self.request.path}", self.__class__.__name__)
        """
        Some applications need to know where they are being proxied from.
        This is either:
        - {base_url}/proxy/{port}
        - {base_url}/proxy/{host}:{port}
        - {base_url}/proxy/absolute/{port}
        - {base_url}/proxy/absolute/{host}:{port}
        - {base_url}/{proxy_base}
        """
        host_and_port = str(port) if host == 'localhost' else host + ":" + str(port)
        if self.proxy_base:
            return url_path_join(self.base_url, self.proxy_base)
        if self.absolute_url:
            return url_path_join(self.base_url, 'proxy', 'absolute', host_and_port)
        else:
            return url_path_join(self.base_url, 'proxy', host_and_port)

    def get_client_uri(self, protocol, host, port, proxied_path):
        context_path = self._get_context_path(host, port)
        if self.absolute_url:
            client_path = url_path_join(context_path, proxied_path)
        else:
            client_path = proxied_path

        # Quote spaces, åäö and such, but only enough to send a valid web
        # request onwards. To do this, we mark the RFC 3986 specs' "reserved"
        # and "un-reserved" characters as safe that won't need quoting. The
        # un-reserved need to be marked safe to ensure the quote function behave
        # the same in py36 as py37.
        #
        # ref: https://tools.ietf.org/html/rfc3986#section-2.2
        client_path = quote(client_path, safe=":/?#[]@!$&'()*+,;=-._~")

        client_uri = '{protocol}://{host}:{port}{path}'.format(
            protocol=protocol,
            host=host,
            port=port,
            path=client_path
        )
        if self.request.query:
            client_uri += '?' + self.request.query

        file_log(f"get_client_uri {client_uri}", self.__class__.__name__)
        return client_uri

    def _build_proxy_request(self, host, port, proxied_path, body):

        headers = self.proxy_request_headers()

        client_uri = self.get_client_uri('http', host, port, proxied_path)
        file_log(f"_build_proxy_request {client_uri}", self.__class__.__name__)
        # Some applications check X-Forwarded-Context and X-ProxyContextPath
        # headers to see if and where they are being proxied from.
        if not self.absolute_url:
            context_path = self._get_context_path(host, port)
            headers['X-Forwarded-Context'] = context_path
            headers['X-ProxyContextPath'] = context_path
            # to be compatible with flask/werkzeug wsgi applications
            headers['X-Forwarded-Prefix'] = context_path

        req = httpclient.HTTPRequest(
            client_uri, method=self.request.method, body=body,
            headers=headers, **self.proxy_request_options())
        return req

    def _check_host_allowlist(self, host):
        file_log(f"_check_host_allowlist {self.request.path}", self.__class__.__name__)
        if callable(self.host_allowlist):
            return self.host_allowlist(self, host)
        else:
            return host in self.host_allowlist

    @web.authenticated
    async def proxy(self, host, port, proxied_path):
        file_log(f"proxy {self.request.path}", self.__class__.__name__)
        '''
        This serverextension handles:
            {base_url}/proxy/{port([0-9]+)}/{proxied_path}
            {base_url}/proxy/absolute/{port([0-9]+)}/{proxied_path}
            {base_url}/{proxy_base}/{proxied_path}
        '''
        file_log(f"calling proxy {self}, {host}, {port}, {proxied_path}", self.__class__.__name__)
        file_log(f"request headers: {self.request.headers}", self.__class__.__name__)

        if not self._check_host_allowlist(host):
            self.set_status(403)
            self.write("Host '{host}' is not allowed. "
                       "See https://jupyter-server-proxy.readthedocs.io/en/latest/arbitrary-ports-hosts.html for info.".format(host=host))
            return

        if 'Proxy-Connection' in self.request.headers:
            del self.request.headers['Proxy-Connection']

        self._record_activity()

        if self.request.headers.get("Upgrade", "").lower() == 'websocket':
            # We wanna websocket!
            # jupyterhub/jupyter-server-proxy@36b3214
            self.log.info("we wanna websocket, but we don't define WebSocketProxyHandler")
            self.set_status(500)

        body = self.request.body
        if not body:
            if self.request.method == 'POST':
                body = b''
            else:
                body = None

        client = httpclient.AsyncHTTPClient()

        req = self._build_proxy_request(host, port, proxied_path, body)

        try:
            response = await client.fetch(req, raise_error=False)
        except httpclient.HTTPError as err:
            # We need to capture the timeout error even with raise_error=False,
            # because it only affects the HTTPError raised when a non-200 response 
            # code is used, instead of suppressing all errors.
            # Ref: https://www.tornadoweb.org/en/stable/httpclient.html#tornado.httpclient.AsyncHTTPClient.fetch
            if err.code == 599:
                self._record_activity()
                self.set_status(599)
                self.write(str(err))
                return
            else:
                raise

        # record activity at start and end of requests
        self._record_activity()

        # For all non http errors...
        if response.error and type(response.error) is not httpclient.HTTPError:
            self.set_status(500)
            self.write(str(response.error))
        else:
            self.set_status(response.code, response.reason)

            # clear tornado default header
            self._headers = httputil.HTTPHeaders()

            for header, v in response.headers.get_all():
                if header not in ('Content-Length', 'Transfer-Encoding',
                                  'Content-Encoding', 'Connection'):
                    # some header appear multiple times, eg 'Set-Cookie'
                    self.add_header(header, v)

            if response.body:
                self.write(response.body)

    async def proxy_open(self, host, port, proxied_path=''):
        file_log(f"proxy_open {self.request.path}", self.__class__.__name__)
        """
        Called when a client opens a websocket connection.

        We establish a websocket connection to the proxied backend &
        set up a callback to relay messages through.
        """

        if not self._check_host_allowlist(host):
            self.set_status(403)
            self.log.info("Host '{host}' is not allowed. "
                          "See https://jupyter-server-proxy.readthedocs.io/en/latest/arbitrary-ports-hosts.html for info.".format(host=host))
            self.close()
            return

        if not proxied_path.startswith('/'):
            proxied_path = '/' + proxied_path

        client_uri = self.get_client_uri('ws', host, port, proxied_path)
        headers = self.request.headers
        current_loop = ioloop.IOLoop.current()
        ws_connected = current_loop.asyncio_loop.create_future()

        def message_cb(message):
            """
            Callback when the backend sends messages to us

            We just pass it back to the frontend
            """
            # Websockets support both string (utf-8) and binary data, so let's
            # make sure we signal that appropriately when proxying
            self._record_activity()
            if message is None:
                self.close()
            else:
                self.write_message(message, binary=isinstance(message, bytes))

        def ping_cb(data):
            """
            Callback when the backend sends pings to us.

            We just pass it back to the frontend.
            """
            self._record_activity()
            self.ping(data)

        async def start_websocket_connection():
            self.log.info('Trying to establish websocket connection to {}'.format(client_uri))
            self._record_activity()
            request = httpclient.HTTPRequest(url=client_uri, headers=headers)
            self.ws = await pingable_ws_connect(request=request,
                on_message_callback=message_cb, on_ping_callback=ping_cb,
                subprotocols=self.subprotocols)
            ws_connected.set_result(True)
            self._record_activity()
            self.log.info('Websocket connection established to {}'.format(client_uri))

        current_loop.add_callback(start_websocket_connection)
        # Wait for the WebSocket to be connected before resolving.
        # Otherwise, messages sent by the client before the
        # WebSocket successful connection would be dropped.
        await ws_connected


    def proxy_request_headers(self):
        file_log(f"proxy_request_headers {self.request.path}", self.__class__.__name__)
        '''A dictionary of headers to be used when constructing
        a tornado.httpclient.HTTPRequest instance for the proxy request.'''
        return self.request.headers.copy()

    def proxy_request_options(self):
        file_log(f"proxy_request_options {self.request.path}", self.__class__.__name__)
        '''A dictionary of options to be used when constructing
        a tornado.httpclient.HTTPRequest instance for the proxy request.'''
        return dict(follow_redirects=False, connect_timeout=250.0, request_timeout=300.0)

    def check_xsrf_cookie(self):
        file_log(f"check_xsrf_cookie {self.request.path}", self.__class__.__name__)
        '''
        http://www.tornadoweb.org/en/stable/guide/security.html

        Defer to proxied apps.
        '''
        pass

    def select_subprotocol(self, subprotocols):
        file_log(f"select_subprotocol {self.request.path}", self.__class__.__name__)
        '''Select a single Sec-WebSocket-Protocol during handshake.'''
        self.subprotocols = subprotocols
        if isinstance(subprotocols, list) and subprotocols:
            self.log.debug('Client sent subprotocols: {}'.format(subprotocols))
            return subprotocols[0]
        return super().select_subprotocol(subprotocols)


class LocalProxyHandler(ProxyHandler):
    """
    A tornado request handler that proxies HTTP and websockets
    from a port on the local system. Same as the above ProxyHandler,
    but specific to 'localhost'.
    """
    async def http_get(self, port, proxied_path):
        file_log(f"http_get {self.request.path}", self.__class__.__name__)
        return await self.proxy(port, proxied_path)

    async def open(self, port, proxied_path):
        file_log(f"open {self.request.path}", self.__class__.__name__)
        return await self.proxy_open('localhost', port, proxied_path)

    def post(self, port, proxied_path):
        file_log(f"post {self.request.path}", self.__class__.__name__)
        return self.proxy(port, proxied_path)

    def put(self, port, proxied_path):
        file_log(f"put {self.request.path}", self.__class__.__name__)
        return self.proxy(port, proxied_path)

    def delete(self, port, proxied_path):
        file_log(f"delete {self.request.path}", self.__class__.__name__)
        return self.proxy(port, proxied_path)

    def head(self, port, proxied_path):
        file_log(f"head {self.request.path}", self.__class__.__name__)
        return self.proxy(port, proxied_path)

    def patch(self, port, proxied_path):
        file_log(f"patch {self.request.path}", self.__class__.__name__)
        return self.proxy(port, proxied_path)

    def options(self, port, proxied_path):
        file_log(f"options {self.request.path}", self.__class__.__name__)
        return self.proxy(port, proxied_path)

    def proxy(self, port, proxied_path):
        file_log(f"proxy {self.request.path}", self.__class__.__name__)
        return super().proxy('localhost', port, proxied_path)


class RemoteProxyHandler(ProxyHandler):
    """
    A tornado request handler that proxies HTTP and websockets
    from a port on a specified remote system.
    """

    async def http_get(self, host, port, proxied_path):
        file_log(f"http_get {self.request.path}", self.__class__.__name__)
        return await self.proxy(host, port, proxied_path)

    def post(self, host, port, proxied_path):
        file_log(f"post {self.request.path}", self.__class__.__name__)
        return self.proxy(host, port, proxied_path)

    def put(self, host, port, proxied_path):
        file_log(f"put {self.request.path}", self.__class__.__name__)
        return self.proxy(host, port, proxied_path)

    def delete(self, host, port, proxied_path):
        file_log(f"delete {self.request.path}", self.__class__.__name__)
        return self.proxy(host, port, proxied_path)

    def head(self, host, port, proxied_path):
        file_log(f"head {self.request.path}", self.__class__.__name__)
        return self.proxy(host, port, proxied_path)

    def patch(self, host, port, proxied_path):
        file_log(f"patch {self.request.path}", self.__class__.__name__)
        return self.proxy(host, port, proxied_path)

    def options(self, host, port, proxied_path):
        file_log(f"options {self.request.path}", self.__class__.__name__)
        return self.proxy(host, port, proxied_path)

    async def open(self, host, port, proxied_path):
        file_log(f"open {self.request.path}", self.__class__.__name__)
        return await self.proxy_open(host, port, proxied_path)

    def proxy(self, host, port, proxied_path):
        file_log(f"proxy {self.request.path}", self.__class__.__name__)
        return super().proxy(host, port, proxied_path)


# FIXME: Move this to its own file. Too many packages now import this from nbrserverproxy.handlers
class SuperviseAndProxyHandler(LocalProxyHandler):
    '''Manage a given process and requests to it '''

    def __init__(self, *args, **kwargs):
        file_log(f"__init__ {self.request.path}", self.__class__.__name__)
        self.requested_port = 0
        self.mappath = {}
        super().__init__(*args, **kwargs)

    def initialize(self, state):
        file_log(f"initialize {self.request.path}", self.__class__.__name__)
        self.state = state
        if 'proc_lock' not in state:
            state['proc_lock'] = Lock()

    name = 'process'

    @property
    def port(self):
        file_log(f"port {self.request.path}", self.__class__.__name__)
        """
        Allocate either the requested port or a random empty port for use by
        application
        """
        if 'port' not in self.state:
            sock = socket.socket()
            sock.bind(('', self.requested_port))
            self.state['port'] = sock.getsockname()[1]
            sock.close()
        return self.state['port']

    def get_cwd(self):
        file_log(f"get_cwd {self.request.path}", self.__class__.__name__)
        """Get the current working directory for our process

        Override in subclass to launch the process in a directory
        other than the current.
        """
        return os.getcwd()

    def get_env(self):
        file_log(f"get_env {self.request.path}", self.__class__.__name__)
        '''Set up extra environment variables for process. Typically
           overridden in subclasses.'''
        return {}

    def get_timeout(self):
        file_log(f"get_timeout {self.request.path}", self.__class__.__name__)
        """
        Return timeout (in s) to wait before giving up on process readiness
        """
        return 5

    async def _http_ready_func(self, p):
        file_log(f"_http_ready_func {self.request.path}", self.__class__.__name__)
        url = 'http://localhost:{}'.format(self.port)
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    # We only care if we get back *any* response, not just 200
                    # If there's an error response, that can be shown directly to the user
                    self.log.debug('Got code {} back from {}'.format(resp.status, url))
                    return True
            except aiohttp.ClientConnectionError:
                self.log.debug('Connection to {} refused'.format(url))
                return False

    async def ensure_process(self):
        file_log(f"ensure_process {self.request.path}", self.__class__.__name__)
        """
        Start the process
        """
        # We don't want multiple requests trying to start the process at the same time
        # FIXME: Make sure this times out properly?
        # Invariant here should be: when lock isn't being held, either 'proc' is in state &
        # running, or not.
        async with self.state['proc_lock']:
            if 'proc' not in self.state:
                # FIXME: Prevent races here
                # FIXME: Handle graceful exits of spawned processes here
                cmd = self.get_cmd()
                server_env = os.environ.copy()

                # Set up extra environment variables for process
                server_env.update(self.get_env())

                timeout = self.get_timeout()

                proc = SupervisedProcess(self.name, *cmd, env=server_env, ready_func=self._http_ready_func, ready_timeout=timeout, log=self.log)
                self.state['proc'] = proc

                try:
                    await proc.start()

                    is_ready = await proc.ready()

                    if not is_ready:
                        await proc.kill()
                        raise web.HTTPError(500, 'could not start {} in time'.format(self.name))
                except:
                    # Make sure we remove proc from state in any error condition
                    del self.state['proc']
                    raise


    @web.authenticated
    async def proxy(self, port, path):
        file_log(f"proxy {self.request.path}", self.__class__.__name__)
        if not path.startswith('/'):
            path = '/' + path
        if self.mappath:
            if callable(self.mappath):
                path = call_with_asked_args(self.mappath, {'path': path})
            else:
                path = self.mappath.get(path, path)

        await self.ensure_process()

        return await ensure_async(super().proxy(self.port, path))


    async def http_get(self, path):
        file_log(f"http_get {self.request.path}", self.__class__.__name__)
        return await ensure_async(self.proxy(self.port, path))

    async def open(self, path):
        file_log(f"open {self.request.path}", self.__class__.__name__)
        await self.ensure_process()
        return await super().open(self.port, path)

    def post(self, path):
        file_log(f"post {self.request.path}", self.__class__.__name__)
        return self.proxy(self.port, path)

    def put(self, path):
        file_log(f"put {self.request.path}", self.__class__.__name__)
        return self.proxy(self.port, path)

    def delete(self, path):
        file_log(f"delete {self.request.path}", self.__class__.__name__)
        return self.proxy(self.port, path)

    def head(self, path):
        file_log(f"head {self.request.path}", self.__class__.__name__)
        return self.proxy(self.port, path)

    def patch(self, path):
        file_log(f"patch {self.request.path}", self.__class__.__name__)
        return self.proxy(self.port, path)

    def options(self, path):
        file_log(f"options {self.request.path}", self.__class__.__name__)
        return self.proxy(self.port, path)


def setup_handlers(web_app, host_allowlist):
    host_pattern = '.*$'
    web_app.add_handlers('.*', [
        (url_path_join(web_app.settings['base_url'], r'/proxy/(.*):(\d+)(.*)'),
         RemoteProxyHandler, {'absolute_url': False, 'host_allowlist': host_allowlist}),
        (url_path_join(web_app.settings['base_url'], r'/proxy/absolute/(.*):(\d+)(.*)'),
         RemoteProxyHandler, {'absolute_url': True, 'host_allowlist': host_allowlist}),
        (url_path_join(web_app.settings['base_url'], r'/proxy/(\d+)(.*)'),
         LocalProxyHandler, {'absolute_url': False}),
        (url_path_join(web_app.settings['base_url'], r'/proxy/absolute/(\d+)(.*)'),
         LocalProxyHandler, {'absolute_url': True}),
    ])

# vim: set et ts=4 sw=4:
