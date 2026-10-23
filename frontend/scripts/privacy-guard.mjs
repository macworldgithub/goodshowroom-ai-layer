import fs from "node:fs";
import net from "node:net";
import dns from "node:dns";
import { Writable } from "node:stream";
import { isAllowedHarnessRequest } from "./test-network-policy.mjs";

const guardKey = Symbol.for("goodshowroom.test.privacy-guard");
const privateDiagnosticName = "error-context.md";
const dnsMethods = [
  "lookup",
  "lookupService",
  "resolve",
  "resolve4",
  "resolve6",
  "resolveAny",
  "resolveCaa",
  "resolveCname",
  "resolveMx",
  "resolveNaptr",
  "resolveNs",
  "resolvePtr",
  "resolveSoa",
  "resolveSrv",
  "resolveTxt",
  "reverse",
];

function blockedNetworkError() {
  const error = new Error("External network access is disabled in tests.");
  error.code = "ERR_TEST_NETWORK_BLOCKED";
  return error;
}

function isHarnessHost(host) {
  return String(host ?? "").toLowerCase().replace(/^\[|\]$/g, "") === "127.0.0.1";
}

function isPrivateDiagnosticPath(target) {
  let path = "";
  if (typeof target === "string") path = target;
  else if (target instanceof URL && target.protocol === "file:") path = target.pathname;
  return path.split(/[\\/]/).at(-1)?.toLowerCase() === privateDiagnosticName;
}

if (process.env.GOODSHOWROOM_PRIVACY_GUARD === "active" && !globalThis[guardKey]) {
  globalThis[guardKey] = true;

  const originalFetch = globalThis.fetch;
  if (typeof originalFetch === "function") {
    globalThis.fetch = function guardedFetch(input, init = {}) {
      let url = "";
      let method = init.method;
      try {
        if (input instanceof Request) {
          url = input.url;
          method ??= input.method;
        } else {
          url = String(input);
        }
      } catch {
        return Promise.reject(blockedNetworkError());
      }
      if (!isAllowedHarnessRequest(url, method ?? "GET")) {
        return Promise.reject(blockedNetworkError());
      }
      return originalFetch.call(this, input, { ...init, redirect: "manual" });
    };
  }

  const socketPrototype = net.Socket.prototype;
  const originalConnect = socketPrototype.connect;
  socketPrototype.connect = function guardedConnect(...args) {
    const first = Array.isArray(args[0]) ? args[0][0] : args[0];
    const host =
      typeof first === "number"
        ? typeof args[1] === "string"
          ? args[1]
          : ""
        : first && typeof first === "object"
          ? first.host ?? ""
          : "";
    const port = typeof first === "number" ? first : first && typeof first === "object" ? first.port : undefined;
    if (!isHarnessHost(host) || Number(port) !== 4178) {
      const error = blockedNetworkError();
      process.nextTick(() => this.destroy(error));
      return this;
    }
    return Reflect.apply(originalConnect, this, args);
  };

  function guardDnsMethod(target, name) {
    const original = target[name];
    if (typeof original !== "function") return;
    target[name] = function guardedDnsMethod(host, ...args) {
      if (isHarnessHost(host)) return Reflect.apply(original, this, [host, ...args]);
      const error = blockedNetworkError();
      const callback = args.at(-1);
      if (typeof callback === "function") {
        process.nextTick(callback, error);
        return undefined;
      }
      return Promise.reject(error);
    };
  }

  for (const name of dnsMethods) {
    guardDnsMethod(dns, name);
    guardDnsMethod(dns.promises, name);
  }

  const fsPromises = fs.promises;
  const originalPromiseWrite = fsPromises.writeFile;
  fsPromises.writeFile = function guardedPromiseWrite(target, ...args) {
    if (isPrivateDiagnosticPath(target)) return Promise.resolve();
    return Reflect.apply(originalPromiseWrite, this, [target, ...args]);
  };

  const originalWriteFile = fs.writeFile;
  fs.writeFile = function guardedWriteFile(target, ...args) {
    if (!isPrivateDiagnosticPath(target)) {
      return Reflect.apply(originalWriteFile, this, [target, ...args]);
    }
    const callback = args.find((value) => typeof value === "function");
    if (callback) process.nextTick(callback, null);
    return callback ? undefined : Promise.resolve();
  };

  const originalWriteFileSync = fs.writeFileSync;
  fs.writeFileSync = function guardedWriteFileSync(target, ...args) {
    if (isPrivateDiagnosticPath(target)) return undefined;
    return Reflect.apply(originalWriteFileSync, this, [target, ...args]);
  };

  const originalCreateWriteStream = fs.createWriteStream;
  fs.createWriteStream = function guardedCreateWriteStream(target, ...args) {
    if (!isPrivateDiagnosticPath(target)) {
      return Reflect.apply(originalCreateWriteStream, this, [target, ...args]);
    }
    return new Writable({ write(_chunk, _encoding, callback) { callback(); } });
  };
}
