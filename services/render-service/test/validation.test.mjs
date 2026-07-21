import test from 'node:test';
import assert from 'node:assert/strict';
import {validateRenderRequest} from '../validation.mjs';

test('normalizes render props and ignores untrusted CSS colors', () => {
  const value = validateRenderRequest({width: 854, height: 480, fps: 24, scenes: [{sceneVersionId: 's1', durationSeconds: 2, purpose: 'Purpose'}], brand: {primary: 'url(evil)', background: '#000000'}});
  assert.equal(value.durationSeconds, 2);
  assert.equal(value.brand.primary, '#6ee7ff');
});

test('rejects empty scene sets and excessive dimensions', () => {
  assert.throws(() => validateRenderRequest({width: 9999, height: 480, fps: 24, scenes: []}), /width/);
});
