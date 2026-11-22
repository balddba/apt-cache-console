import assert from 'node:assert/strict';
import test from 'node:test';
import { copyText } from '../app/static/clipboard.mjs';

/** Install a clipboard stub and restore the original property after the test. */
function stubClipboard(t, value) {
  const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value });
  t.after(() => {
    if (original) Object.defineProperty(navigator, 'clipboard', original);
    else delete navigator.clipboard;
  });
}

test('copies commands with the modern API when available', async (t) => {
  const command = 'first line\nsecond line';
  stubClipboard(t, {
    writeText: async (text) => assert.equal(text, command),
  });
  assert.equal(await copyText(command), true);
});

for (const apiState of ['unavailable', 'denied']) {
  for (const result of [true, false, 'throws']) {
    test(`uses fallback when Clipboard API is ${apiState} and copy returns ${result}`, async (t) => {
      const command = 'printf "hello"\nsudo apt-get update';
      const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document');
      t.after(() => {
        if (originalDocument) Object.defineProperty(globalThis, 'document', originalDocument);
        else delete globalThis.document;
      });
      stubClipboard(t, apiState === 'unavailable' ? undefined : {
        writeText: async () => { throw new Error('Permission denied'); },
      });
      let focused = false;
      let selected = false;
      let removed = false;
      let restored = false;
      const textarea = {
        focus: () => { focused = true; },
        select: () => { selected = true; },
        remove: () => { removed = true; },
      };
      globalThis.document = {
        activeElement: { focus: () => { restored = true; } },
        createElement: (tag) => { assert.equal(tag, 'textarea'); return textarea; },
        body: { append: (node) => assert.equal(node, textarea) },
        execCommand: (action) => {
          assert.equal(action, 'copy');
          assert.equal(textarea.value, command);
          assert.ok(focused && selected);
          if (result === 'throws') throw new Error('Copy blocked');
          return result;
        },
      };
      assert.equal(await copyText(command), result === true);
      assert.ok(removed && restored);
    });
  }
}
