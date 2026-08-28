/**
 * Motet - Motet UI Common - Conversation Auth Identity Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Unit tests for principal identity and conversation-list sync planning.
 *     Run: npx tsx --test src/hooks/conversationAuth.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  authIdentityKey,
  jwtSubject,
  planConversationListSync,
} from "./conversationAuth";
import { defaultAuthState, type AuthState } from "../types";

function makeJwt(payload: Record<string, unknown>): string {
  const header = Buffer.from(JSON.stringify({ alg: "none", typ: "JWT" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return `${header}.${body}.sig`;
}

function auth(overrides: Partial<AuthState>): AuthState {
  return { ...defaultAuthState, ...overrides };
}

test("jwtSubject reads sub from a compact JWT", () => {
  assert.equal(jwtSubject(makeJwt({ sub: "user-123" })), "user-123");
});

test("jwtSubject returns null for malformed tokens", () => {
  assert.equal(jwtSubject("not-a-jwt"), null);
  assert.equal(jwtSubject(makeJwt({ aud: "only-aud" })), null);
});

test("authIdentityKey is stable across JWT refresh for the same sub", () => {
  const first = auth({ jwt: makeJwt({ sub: "alice", exp: 1 }) });
  const refreshed = auth({ jwt: makeJwt({ sub: "alice", exp: 2 }) });
  assert.equal(authIdentityKey(first), "jwt:alice");
  assert.equal(authIdentityKey(refreshed), authIdentityKey(first));
});

test("authIdentityKey changes when JWT sub changes", () => {
  const alice = auth({ jwt: makeJwt({ sub: "alice" }) });
  const bob = auth({ jwt: makeJwt({ sub: "bob" }) });
  assert.notEqual(authIdentityKey(alice), authIdentityKey(bob));
});

test("authIdentityKey uses API key when no JWT is present", () => {
  assert.equal(authIdentityKey(auth({ apiKey: "k-1" })), "key:k-1");
});

test("planConversationListSync: first mount keeps cache and loads from API", () => {
  assert.deepEqual(
    planConversationListSync({
      identity: "jwt:alice",
      prevIdentity: null,
      scope: "core.default:demo_chat",
      prevScope: null,
    }),
    { load: true, replace: true, resetLocal: false, restoreCached: false },
  );
});

test("planConversationListSync: JWT rotation is a no-op", () => {
  assert.deepEqual(
    planConversationListSync({
      identity: "jwt:alice",
      prevIdentity: "jwt:alice",
      scope: "core.default:demo_chat",
      prevScope: "core.default:demo_chat",
    }),
    { load: false, replace: false, resetLocal: false, restoreCached: false },
  );
});

test("planConversationListSync: logout resets locally and does not fetch", () => {
  assert.deepEqual(
    planConversationListSync({
      identity: "",
      prevIdentity: "jwt:alice",
      scope: "core.default:demo_chat",
      prevScope: "core.default:demo_chat",
    }),
    { load: false, replace: false, resetLocal: true, restoreCached: false },
  );
});

test("planConversationListSync: login after logout resets then replaces", () => {
  assert.deepEqual(
    planConversationListSync({
      identity: "jwt:bob",
      prevIdentity: "",
      scope: "core.default:demo_chat",
      prevScope: null,
    }),
    { load: true, replace: true, resetLocal: true, restoreCached: false },
  );
});

test("planConversationListSync: different principal resets then replaces", () => {
  assert.deepEqual(
    planConversationListSync({
      identity: "jwt:bob",
      prevIdentity: "jwt:alice",
      scope: "core.default:demo_chat",
      prevScope: "core.default:demo_chat",
    }),
    { load: true, replace: true, resetLocal: true, restoreCached: false },
  );
});

test("planConversationListSync: scope change restores cache then replaces", () => {
  assert.deepEqual(
    planConversationListSync({
      identity: "jwt:alice",
      prevIdentity: "jwt:alice",
      scope: "core.default:ops_dashboard",
      prevScope: "core.default:demo_chat",
    }),
    { load: true, replace: true, resetLocal: false, restoreCached: true },
  );
});
