/**
 * A minimal Socket.IO client — just enough of the protocol to subscribe to a case and
 * receive the events the detector pushes back.
 *
 * The official `socket.io-client` is the thing to reach for in a real frontend; it adds
 * reconnection, HTTP long-polling fallback, acks, rooms and multiplexing. None of that
 * is needed here, and vendoring it would mean a build step and a package registry for a
 * page that otherwise has neither. So this covers the WebSocket transport only, which is
 * what the server negotiates anyway.
 *
 * Two layers are in play, and both put their type in the first character(s):
 *
 *   Engine.IO   0 open   1 close   2 ping   3 pong   4 message
 *   Socket.IO   (inside a "4") 0 connect  1 disconnect  2 event  4 connect_error
 *
 * So `42["case",{...}]` reads as: Engine.IO message, Socket.IO event, named "case".
 */

const EIO = { OPEN: '0', CLOSE: '1', PING: '2', PONG: '3', MESSAGE: '4' };
const SIO = { CONNECT: '0', DISCONNECT: '1', EVENT: '2', CONNECT_ERROR: '4' };

/** Turn one wire frame into something the caller can switch on. */
function decode(raw) {
  if (raw[0] === EIO.PING) return { kind: 'ping' };
  if (raw[0] === EIO.OPEN) return { kind: 'open', data: JSON.parse(raw.slice(1)) };
  if (raw[0] === EIO.CLOSE) return { kind: 'close' };
  if (raw[0] !== EIO.MESSAGE) return { kind: 'ignored' };

  const type = raw[1];
  if (type === SIO.CONNECT) return { kind: 'connected' };
  if (type === SIO.DISCONNECT) return { kind: 'disconnected' };
  if (type === SIO.CONNECT_ERROR) return { kind: 'error', data: raw.slice(2) };
  if (type !== SIO.EVENT) return { kind: 'ignored' };

  // An event may carry an ack id between the type and the payload: 42["x"] or 4217["x"].
  const [name, payload] = JSON.parse(raw.slice(2).replace(/^\d+/, ''));
  return { kind: 'event', name, payload };
}

/**
 * Open a connection. Returns a handle with `emit`, `on` and `close`.
 *
 * `baseUrl` is an http(s) origin; the ws(s) URL is derived from it, so the caller never
 * has to think about which scheme goes with which.
 */
export function connect(baseUrl, { onOpen, onClose, onError } = {}) {
  const url = new URL('/socket.io/', baseUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('EIO', '4');
  url.searchParams.set('transport', 'websocket');

  const socket = new WebSocket(url);
  const handlers = new Map();
  let ready = false;
  const queued = [];

  const send = (frame) => socket.readyState === WebSocket.OPEN && socket.send(frame);

  socket.addEventListener('message', ({ data }) => {
    const packet = decode(data);
    switch (packet.kind) {
      case 'open':
        // The server is ready at the Engine.IO layer; now join the default namespace.
        send(EIO.MESSAGE + SIO.CONNECT);
        break;
      case 'connected':
        ready = true;
        queued.splice(0).forEach(send);
        onOpen?.();
        break;
      case 'ping':
        send(EIO.PONG); // Miss these and the server drops the connection.
        break;
      case 'event':
        handlers.get(packet.name)?.forEach((fn) => fn(packet.payload));
        break;
      case 'error':
        onError?.(new Error(`connect_error: ${packet.data}`));
        break;
      case 'disconnected':
      case 'close':
        socket.close();
        break;
    }
  });

  socket.addEventListener('error', () => onError?.(new Error('the connection failed')));
  socket.addEventListener('close', () => onClose?.());

  return {
    /** Register a handler for one server event. */
    on(name, fn) {
      handlers.set(name, [...(handlers.get(name) ?? []), fn]);
      return this;
    },
    /** Send an event. Queued until the namespace handshake finishes. */
    emit(name, payload) {
      const frame = EIO.MESSAGE + SIO.EVENT + JSON.stringify([name, payload]);
      ready ? send(frame) : queued.push(frame);
      return this;
    },
    close() {
      send(EIO.MESSAGE + SIO.DISCONNECT);
      socket.close();
    },
  };
}
