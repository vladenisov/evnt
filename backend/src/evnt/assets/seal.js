/*!
 * evnt encrypted transport for the Snowplow browser tracker.
 *
 * Served by the collector at `<encryption endpoint>.js` with the recipient key
 * and key id already substituted, so no key material has to live in a tag
 * manager container or an application bundle. See `evnt/core/crypto.py` for the
 * wire format this file has to match byte for byte.
 *
 * Usage:
 *
 *     <script async src="https://collector.example/e.js"></script>
 *     snowplow('newTracker', 'sp1', 'https://collector.example', {
 *       postPath: '/tracker',
 *       dontRetryStatusCodes: [413],
 *       customFetch: function (request) {
 *         var f = window.evnt && window.evnt.encryptedFetch;
 *         if (f) return f(request);
 *         return Promise.reject(new Error('evnt: sealer not loaded'));
 *       },
 *     });
 *
 * The rejection above is deliberate: the tracker catches it and keeps the
 * batch for the next flush, whereas leaving `customFetch` undefined would make
 * it fall back to plain `fetch` and post the payload in the clear.
 */
(function (global) {
  "use strict";

  var CONFIG = __EVNT_CONFIG__;

  var MAGIC = [0x45, 0x56, 0x4e, 0x31]; // "EVN1"
  var INFO = "evnt/e/v1";
  var VERSION = 1;
  var FLAG_GZIP = 0x01;
  var NONCE_BYTES = 12;
  var TAG_BITS = 128;

  var subtle = global.crypto && global.crypto.subtle;

  // Resolved against this script's own URL rather than the page's, because the
  // collector is on a different origin from the site in every real deployment.
  // `document.currentScript` is read here, at top-level execution, since it is
  // null by the time any of the promises below run.
  var currentScript = global.document && global.document.currentScript;
  var ENDPOINT = new URL(
    CONFIG.endpoint,
    (currentScript && currentScript.src) || (global.location && global.location.href),
  ).toString();

  function bytes(text) {
    return new TextEncoder().encode(text);
  }

  function concat(parts) {
    var total = 0;
    var i;
    for (i = 0; i < parts.length; i++) total += parts[i].length;
    var out = new Uint8Array(total);
    var off = 0;
    for (i = 0; i < parts.length; i++) {
      out.set(parts[i], off);
      off += parts[i].length;
    }
    return out;
  }

  function fromBase64(value) {
    return Uint8Array.from(global.atob(value), function (c) {
      return c.charCodeAt(0);
    });
  }

  var RPK = fromBase64(CONFIG.publicKey);
  var KID = bytes(CONFIG.kid);
  var INFO_BYTES = bytes(INFO);

  /*
   * X25519 landed in WebCrypto only in Chrome 133, Firefox 130 and Safari 17,
   * so support is probed once rather than assumed. Older browsers keep posting
   * to the plaintext endpoint instead of losing their events: shipping a
   * userspace curve implementation would cost ~10 KB gzipped to encrypt a
   * payload whose sealing key is readable in the page anyway.
   */
  var supportPromise = null;

  function supported() {
    if (supportPromise === null) {
      supportPromise = (function () {
        if (!subtle) return Promise.resolve(false);
        try {
          return subtle
            .generateKey({ name: "X25519" }, true, ["deriveBits"])
            .then(function () {
              return true;
            })
            .catch(function () {
              return false;
            });
        } catch (err) {
          return Promise.resolve(false);
        }
      })();
    }
    return supportPromise;
  }

  function gzip(input) {
    var stream = new Blob([input]).stream().pipeThrough(new CompressionStream("gzip"));
    return new Response(stream).arrayBuffer().then(function (buffer) {
      return new Uint8Array(buffer);
    });
  }

  function agree() {
    return subtle
      .importKey("raw", RPK, { name: "X25519" }, false, [])
      .then(function (peer) {
        return subtle
          .generateKey({ name: "X25519" }, true, ["deriveBits"])
          .then(function (ephemeral) {
            return Promise.all([
              subtle.exportKey("raw", ephemeral.publicKey),
              subtle.deriveBits({ name: "X25519", public: peer }, ephemeral.privateKey, 256),
            ]);
          });
      })
      .then(function (pair) {
        return { epk: new Uint8Array(pair[0]), shared: new Uint8Array(pair[1]) };
      });
  }

  function deriveKey(epk) {
    // An empty salt is what the collector's `salt=None` means: HMAC pads a
    // short key with zeroes to the block size, so both sides key HKDF's
    // extract step identically.
    return subtle
      .importKey("raw", epk.shared, "HKDF", false, ["deriveBits"])
      .then(function (ikm) {
        return subtle.deriveBits(
          {
            name: "HKDF",
            hash: "SHA-256",
            salt: new Uint8Array(0),
            info: concat([INFO_BYTES, epk.epk, RPK]),
          },
          ikm,
          256,
        );
      })
      .then(function (raw) {
        return subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt"]);
      });
  }

  /**
   * Seal a payload into the collector's envelope.
   *
   * @param {Uint8Array} plaintext Bytes to seal
   * @param {boolean} [compress] Gzip before sealing and set the matching flag
   * @returns {Promise<Uint8Array>} The complete envelope, ready to POST
   */
  function seal(plaintext, compress) {
    var wantsGzip = compress !== false && CONFIG.compress && typeof CompressionStream !== "undefined";
    var prepared = wantsGzip ? gzip(plaintext) : Promise.resolve(plaintext);

    return prepared.then(function (body) {
      var flags = wantsGzip ? FLAG_GZIP : 0;
      return agree().then(function (epk) {
        return deriveKey(epk).then(function (key) {
          var header = concat([
            new Uint8Array(MAGIC),
            new Uint8Array([VERSION, flags, KID.length]),
            KID,
            epk.epk,
          ]);
          var nonce = global.crypto.getRandomValues(new Uint8Array(NONCE_BYTES));
          return subtle
            .encrypt(
              { name: "AES-GCM", iv: nonce, additionalData: header, tagLength: TAG_BITS },
              key,
              body,
            )
            .then(function (ciphertext) {
              return concat([header, nonce, new Uint8Array(ciphertext)]);
            });
        });
      });
    });
  }

  var warned = false;

  function warnOnce(message, error) {
    if (warned) return;
    warned = true;
    if (global.console && global.console.warn) global.console.warn(message, error || "");
  }

  /**
   * Drop-in replacement for the tracker's `customFetch`.
   *
   * @param {Request} request The request the tracker would have sent
   * @returns {Promise<Response>}
   */
  function encryptedFetch(request) {
    // The tracker routes its idService / cookieExtensionService GET through
    // `customFetch` too; only event batches are ours to seal.
    if (request.method !== "POST") return fetch(request);

    return supported().then(function (ok) {
      if (!ok) {
        warnOnce("evnt: X25519 unavailable, sending events unencrypted");
        return fetch(request);
      }
      return request
        .text()
        .then(function (body) {
          return seal(bytes(body));
        })
        .then(function (envelope) {
          var headers = new Headers(request.headers);
          headers.set("Content-Type", "application/octet-stream");
          return fetch(ENDPOINT, {
            method: "POST",
            body: envelope,
            headers: headers,
            credentials: request.credentials,
            keepalive: request.keepalive,
            signal: request.signal,
            mode: "cors",
          });
        })
        .catch(function (error) {
          // 400 is in the tracker's dontRetryStatusCodes, so the batch is
          // dropped rather than retried forever against a broken sealer.
          warnOnce("evnt: sealing failed, dropping batch", error);
          return new Response(null, { status: 400 });
        });
    });
  }

  var evnt = global.evnt || (global.evnt = {});
  evnt.encryptedFetch = encryptedFetch;
  evnt.seal = seal;
  evnt.supported = supported;
  evnt.kid = CONFIG.kid;
  evnt.endpoint = ENDPOINT;
})(typeof window !== "undefined" ? window : globalThis);
