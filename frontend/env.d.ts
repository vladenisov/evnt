/// <reference types="vite/client" />

interface SnowplowQueue {
  (...args: unknown[]): void;
  q?: unknown[];
}

/** The sealer the collector serves at `/e.js`, once it has loaded. */
interface EvntSealer {
  encryptedFetch: (request: Request) => Promise<Response>;
  seal: (plaintext: Uint8Array, compress?: boolean) => Promise<Uint8Array>;
  supported: () => Promise<boolean>;
  kid: string;
  endpoint: string;
}

declare global {
  interface Window {
    snowplow: SnowplowQueue;
    GlobalSnowplowNamespace?: string[];
    evnt?: EvntSealer;
  }
}

export {};
