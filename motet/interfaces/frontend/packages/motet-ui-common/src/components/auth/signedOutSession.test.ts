/**
 * Motet - Motet UI Common - Signed-Out Session Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Unit tests for the signed-out session flag helpers.
 *     Run: npx tsx --test src/components/auth/signedOutSession.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  SIGNED_OUT_STORAGE_KEY,
  clearSignedOutFlag,
  markSignedOut,
  wasSignedOut,
} from "./signedOutSession";

const store = new Map<string, string>();

function installSessionStorage(): void {
  const memory: Storage = {
    get length() {
      return store.size;
    },
    clear() {
      store.clear();
    },
    getItem(key: string) {
      return store.has(key) ? store.get(key)! : null;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string) {
      store.delete(key);
    },
    setItem(key: string, value: string) {
      store.set(key, value);
    },
  };
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    value: memory,
  });
}

installSessionStorage();

test("wasSignedOut is false until markSignedOut", () => {
  store.clear();
  assert.equal(wasSignedOut(), false);
  markSignedOut();
  assert.equal(wasSignedOut(), true);
  assert.equal(store.get(SIGNED_OUT_STORAGE_KEY), "1");
});

test("clearSignedOutFlag drops the session marker", () => {
  store.clear();
  markSignedOut();
  clearSignedOutFlag();
  assert.equal(wasSignedOut(), false);
  assert.equal(store.has(SIGNED_OUT_STORAGE_KEY), false);
});
