import test from 'node:test';
import assert from 'node:assert/strict';
import {validateRenderRequest} from '../validation.mjs';

test('normalizes render props and ignores untrusted CSS colors', () => {
  const value = validateRenderRequest({width: 854, height: 480, fps: 24, scenes: [{sceneVersionId: 's1', durationSeconds: 2, purpose: 'Purpose'}], brand: {primary: 'url(evil)', background: '#000000'}});
  assert.equal(value.durationSeconds, 2);
  assert.equal(value.brand.primary, '#174C3C');
});

test('rejects empty scene sets and excessive dimensions', () => {
  assert.throws(() => validateRenderRequest({width: 9999, height: 480, fps: 24, scenes: []}), /width/);
});

test('accepts only hashed scene assets from the internal media origin', () => {
  const value = validateRenderRequest({width: 854, height: 480, fps: 24, scenes: [{sceneVersionId: 's1', durationSeconds: 2, purpose: 'Purpose', assetUrl: 'http://minio:9000/production-artifacts/productions/p/scenes/1.png?signature=test', assetMimeType: 'image/png', assetHash: 'a'.repeat(64)}], brand: {name: 'Channel'}});
  assert.equal(value.scenes[0].assetHash, 'a'.repeat(64));
  assert.match(value.scenes[0].assetUrl, /^http:\/\/minio:9000\/production-artifacts\//);
  assert.throws(() => validateRenderRequest({width: 854, height: 480, fps: 24, scenes: [{sceneVersionId: 's1', durationSeconds: 2, assetUrl: 'http://evil.test/image.png', assetMimeType: 'image/png', assetHash: 'a'.repeat(64)}]}), /internal media origin/);
});

test('normalizes the constrained Channel Brand Kit without accepting CSS', () => {
  const value = validateRenderRequest({width: 854, height: 480, fps: 24, scenes: [{sceneVersionId: 's1', durationSeconds: 2}], brand: {logo_text: 'FS', accent: '#D6A43A', heading_font: 'serif', motion_style: 'calm', corner_style: 'soft', image_treatment: 'editorial'}});
  assert.equal(value.brand.logoText, 'FS');
  assert.equal(value.brand.accent, '#D6A43A');
  assert.equal(value.brand.headingFont, 'serif');
});
