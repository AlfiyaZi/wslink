r"""websocket is a module that provide classes that extend any
twisted/autobahn websocket related class for the purpose of vtkrpc.

"""

from __future__ import absolute_import, division, print_function

import inspect, logging, json

from twisted.web            import resource
from twisted.python         import log
from twisted.internet       import reactor
from twisted.internet       import defer
from twisted.internet.defer import Deferred, returnValue

from . import register as exportRpc
from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketServerProtocol

# =============================================================================
#
# Base class for vtkWeb ServerProtocol
#
# =============================================================================

class ServerProtocol(object):
    """
    Defines the core server protocol for vtkrpc. Gathers a list of vtkWebProtocol
    objects that provide rpc and pub/sub functionality.
    """

    def __init__(self):
        self.vtkWebProtocols = []
        self.secret = None
        self.initialize()

    def init(self, publish, addAttachment):
        self.publish = publish
        self.addAttachment = addAttachment

    def initialize(self):
        """
        Let the sub class define what they need to do to properly initialize
        themselves.
        """
        pass

    # def onJoin(self, details):
    #     self.register(self)
    #     for protocol in self.vtkWebProtocols:
    #         self.register(protocol)

    def registerVtkWebProtocol(self, protocol):
        protocol.coreServer = self
        self.vtkWebProtocols.append(protocol)

    def getVtkWebProtocols(self):
        return self.vtkWebProtocols

    def updateSecret(self, newSecret):
        self.secret = newSecret

    @exportRpc("application.exit")
    def exit(self):
        """RPC callback to exit"""
        reactor.stop()

    @exportRpc("application.exit.later")
    def exitLater(self, secondsLater=60):
        """RPC callback to exit after a short delay"""
        reactor.callLater(secondsLater, reactor.stop)

# =============================================================================
#
# Base class for vtkWeb WebSocketServerFactory
#
# =============================================================================

class TimeoutWebSocketServerFactory(WebSocketServerFactory):
    """
    TimeoutWebSocketServerFactory is WebSocketServerFactory subclass
    that adds support to close the web-server after a timeout when the last
    connected client drops.

    Currently, the protocol must call connectionMade() and connectionLost() methods
    to notify this object that the connection was started/closed.
    If the connection count drops to zero, then the reap timer
    is started which will end the process if no other connections are made in
    the timeout interval.
    """

    def __init__(self, *args, **kwargs):
        self._connection_count = 0
        self.clientCount = 0
        self._timeout = kwargs['timeout']
        self._reaper = reactor.callLater(self._timeout, lambda: reactor.stop())
        self._protocolHandler = None

        del kwargs['timeout']
        WebSocketServerFactory.__init__(self, *args, **kwargs)
        WebSocketServerFactory.protocol = TimeoutWebSocketServerProtocol

    def connectionMade(self):
        if self._reaper:
            log.msg("Client has reconnected, cancelling reaper", logLevel=logging.DEBUG)
            self._reaper.cancel()
            self._reaper = None
        self._connection_count += 1
        self.clientCount += 1
        log.msg("on_connect: connection count = %s" % self._connection_count, logLevel=logging.DEBUG)

    def connectionLost(self, reason):
        if self._connection_count > 0:
            self._connection_count -= 1
        log.msg("connection_lost: connection count = %s" % self._connection_count, logLevel=logging.DEBUG)

        if self._connection_count == 0 and not self._reaper:
            log.msg("Starting timer, process will terminate in: %ssec" % self._timeout, logLevel=logging.DEBUG)
            self._reaper = reactor.callLater(self._timeout, lambda: reactor.stop())

    def setServerProtocol(self, newServerProtocol):
        self._protocolHandler = newServerProtocol

    def getServerProtocol(self):
        return self._protocolHandler

    def getClientCount(self):
        return self.clientCount

# =============================================================================

class TimeoutWebSocketServerProtocol(WebSocketServerProtocol):

    def connectionMade(self):
        WebSocketServerProtocol.connectionMade(self)
        print(self.factory)
        self.factory.connectionMade()

    def connectionLost(self, reason):
        WebSocketServerProtocol.connectionLost(self, reason)
        self.factory.connectionLost(reason)

# from http://www.jsonrpc.org/specification, section 5.1
METHOD_NOT_FOUND = -32601
AUTHENTICATION_ERROR = -32000
EXCEPTION_ERROR = -32001
RESULT_SERIALIZE_ERROR = -32002

# -----------------------------------------------------------------------------
# WS protocol definition
# -----------------------------------------------------------------------------

class VtkrpcWebSocketServerProtocol(TimeoutWebSocketServerProtocol):
    def __init__(self):
        super(VtkrpcWebSocketServerProtocol, self).__init__()
        self.functionMap = {}
        self.attachmentMap = {}
        self.attachmentId = 0
        self.publishCount = 0

    def onConnect(self, request):
        self.clientID = self.factory.getClientCount()
        print("client connected", self.clientID, request)
        # Build the rpc method dictionary. self.factory isn't set until connected.
        protocolList = (self.factory.getServerProtocol().getVtkWebProtocols() if self.factory.getServerProtocol() else [])
        for protocolObject in protocolList:
            protocolObject.init(self.publish, self.addAttachment)
            test = lambda x: inspect.ismethod(x) or inspect.isfunction(x)
            for k in inspect.getmembers(protocolObject.__class__, test):
                proc = k[1]
                if "_wampuris" in proc.__dict__:
                    pat = proc.__dict__["_wampuris"][0]
                    if pat.is_endpoint():
                        uri = pat.uri()
                        self.functionMap[uri] = (protocolObject, proc)

    def onClose(self, wasClean, code, reason):
        print("client closed", wasClean, code, reason)

    def handleSystemMessage(self, rpcid, methodName, args):
        rpcList = rpcid.split(":")
        if rpcList[0] == "system":
            if (methodName == "vtkrpc.hello"):
                if (args and args[0] and (type(args[0]) is dict) and ("secret" in args[0]) \
                    and (args[0]["secret"] == self.factory.getServerProtocol().secret)):
                    self.sendWrappedMessage(rpcid, { "clientID": "c{0}".format(self.clientID) })
                else:
                    self.sendWrappedError(rpcid, AUTHENTICATION_ERROR, "Authentication failed")
            else:
                self.sendWrappedError(rpcid, METHOD_NOT_FOUND, "Unknown system method called")
            return True
        return False


    def onMessage(self, payload, isBinary):
        if isBinary:
            print("Dropping binary message")
            return
        rpc = json.loads(payload)
        # TODO validate
        version = rpc['vtkrpc']
        rpcid = rpc['id']
        methodName = rpc['method']
        args = []
        kwargs = {}
        if ('args' in rpc) and isinstance(rpc['args'], list):
            args = rpc['args']
        if ('kwargs' in rpc) and isinstance(rpc['kwargs'], dict):
            kwargs = rpc['kwargs']

        # Check for system messages, like hello
        if (self.handleSystemMessage(rpcid, methodName, args)):
            return

        if (not methodName in self.functionMap):
            self.sendWrappedError(rpcid, METHOD_NOT_FOUND, "Unregistered method called", methodName)
            return

        obj,func = self.functionMap[methodName]
        try:
            results = func(obj, *args, **kwargs)
        except Exception as e:
            self.sendWrappedError(rpcid, EXCEPTION_ERROR, "Exception raised", repr(e))
            return

        self.sendWrappedMessage(rpcid, results, method=methodName)
        # TODO is this enough to clear the attachment map? Not if publish is called without RPC calls.
        self.attachmentMap.clear()

    def sendWrappedMessage(self, rpcid, content, method=''):
        wrapper = {
            "vtkrpc": "1.0",
            "id": rpcid,
            "result": content,
        }
        try:
            encMsg = json.dumps(wrapper, ensure_ascii = False).encode('utf8')
        except TypeError as e:
            # the content which is not serializable might be arbitrarily large, don't include.
            # repr(content) would do that...
            self.sendWrappedError(rpcid, RESULT_SERIALIZE_ERROR, "Method result cannot be serialized", method)
            return

        # Check if any attachments in the map go with this message
        if self.attachmentMap:
            for key in self.attachmentMap:
                # string match the encoded attachment key
                if key.encode('utf8') in encMsg:
                    # send header
                    header = {
                        "vtkrpc": "1.0",
                        "method": "vtkrpc.binary.attachment",
                        "args": [key],
                    }
                    self.sendMessage(json.dumps(header, ensure_ascii = False).encode('utf8'))
                    # Send binary message
                    self.sendMessage(self.attachmentMap[key], True)

        self.sendMessage(encMsg)

    def sendWrappedError(self, rpcid, code, message, data = None):
        wrapper = {
            "vtkrpc": "1.0",
            "id": rpcid,
            "error": {
                "code": code,
                "message": message,
            },
        }
        if (data):
            wrapper["error"]["data"] = data
        encMsg = json.dumps(wrapper, ensure_ascii = False).encode('utf8')
        self.sendMessage(encMsg)


    def publish(self, topic, data):
        # The client is unknown - we send to any client who is subscribed to the topic
        rpcid = 'publish:{0}:{1}'.format(topic, self.publishCount)
        self.publishCount += 1
        self.sendWrappedMessage(rpcid, data)

    def addAttachment(self, payload):
        print("attachment", self, self.attachmentId)
        # use a string flag in place of the binary attachment.
        # (Using rpcid would prevent re-use of the attachment in publish)
        binaryId = 'vtkrpc_bin{0}'.format(self.attachmentId)
        self.attachmentMap[binaryId] = payload
        self.attachmentId += 1
        return binaryId

    def setSecret(self, newSecret):
        self.secret = newSecret
