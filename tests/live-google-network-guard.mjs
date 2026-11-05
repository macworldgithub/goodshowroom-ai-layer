// The live login test permits only loopback and the Google identity hosts.
import dns from "node:dns";
import net from "node:net";
import dgram from "node:dgram";

const hosts = new Set(["accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com", "openidconnect.googleapis.com"]);
const addresses = new Set();
for (const name of ["resolve", "resolve4", "resolve6", "resolveAny", "reverse", "lookupService"]) {
  dns[name] = () => { throw new Error("Test DNS denied"); };
}
for (const name of Object.keys(dns.promises)) {
  if (typeof dns.promises[name] === "function") dns.promises[name] = () => Promise.reject(new Error("Test DNS denied"));
}
dgram.createSocket = () => { throw new Error("Test UDP denied"); };
const lookup = dns.lookup;
dns.lookup = function (hostname, options, callback) {
  if (typeof options === "function") { callback = options; options = {}; }
  if (hostname === "localhost" || hostname === "127.0.0.1") return lookup.call(this, hostname, options, callback);
  if (!hosts.has(hostname)) return process.nextTick(callback, new Error("Test network denied"));
  return lookup.call(this, hostname, { ...options, all: true }, (error, found) => {
    if (error) return callback(error);
    for (const entry of found) addresses.add(entry.address);
    if (options?.all) return callback(null, found);
    return callback(null, found[0].address, found[0].family);
  });
};
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const first = Array.isArray(args[0]) ? args[0][0] : args[0];
  const host = typeof first === "object" ? first?.host : args[1];
  const port = Number(typeof first === "object" ? first?.port : first);
  if (!((host === "127.0.0.1" || host === "localhost" || host === "::1") && [3000, 8000].includes(port)) && !(port === 443 && (hosts.has(host) || addresses.has(host)))) throw new Error("Test network denied");
  return Reflect.apply(connect, this, args);
};
