const SAFE_COLOR = /^#[0-9a-fA-F]{6}$/;
const SAFE_HASH = /^[0-9a-f]{64}$/;
const ASSET_ORIGIN = new URL(process.env.MEDIA_ASSET_ORIGIN ?? 'http://minio:9000').origin;
const ASSET_BUCKET = process.env.MEDIA_ASSET_BUCKET ?? 'production-artifacts';
const FONT_FAMILIES = new Set(['sans', 'serif', 'rounded', 'mono']);
const CORNER_STYLES = new Set(['square', 'soft', 'rounded']);
const MOTION_STYLES = new Set(['still', 'calm', 'dynamic']);
const IMAGE_TREATMENTS = new Set(['clean', 'editorial', 'documentary', 'cinematic']);
const VISUAL_TYPES = new Set(['title_card', 'citation_card', 'text', 'diagram', 'timeline', 'chart', 'source_screenshot', 'licensed_media', 'comfyui_image', 'comfyui_video', 'waveform', 'branded_transition']);
const ASSET_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'video/mp4', 'video/webm']);

const safeChoice = (value, allowed, fallback) => allowed.has(value) ? value : fallback;
const safeColor = (value, fallback) => SAFE_COLOR.test(value ?? '') ? value.toUpperCase() : fallback;
const safeText = (value, maximum, fallback = '') => String(value ?? fallback).slice(0, maximum);

const safeAssetUrl = (value) => {
  if (value === undefined || value === null || value === '') return null;
  const parsed = new URL(String(value));
  if (parsed.origin !== ASSET_ORIGIN || !parsed.pathname.startsWith(`/${ASSET_BUCKET}/`)) throw new Error('scene asset URL is outside the internal media origin');
  if (parsed.username || parsed.password || parsed.hash) throw new Error('scene asset URL is invalid');
  return parsed.toString();
};

export const validateRenderRequest = (value) => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('request must be an object');
  const {width, height, fps, scenes, brand} = value;
  if (!Number.isInteger(width) || width < 64 || width > 3840) throw new Error('invalid width');
  if (!Number.isInteger(height) || height < 64 || height > 2160) throw new Error('invalid height');
  if (!Number.isInteger(fps) || fps < 1 || fps > 60) throw new Error('invalid fps');
  if (!Array.isArray(scenes) || scenes.length < 1 || scenes.length > 500) throw new Error('invalid scenes');
  let durationSeconds = 0;
  const normalizedScenes = scenes.map((scene) => {
    if (!scene || typeof scene !== 'object' || typeof scene.sceneVersionId !== 'string') throw new Error('invalid scene');
    if (typeof scene.durationSeconds !== 'number' || scene.durationSeconds < 0.5 || scene.durationSeconds > 900) throw new Error('invalid scene duration');
    durationSeconds += scene.durationSeconds;
    const assetUrl = safeAssetUrl(scene.assetUrl);
    const assetMimeType = scene.assetMimeType === undefined ? null : String(scene.assetMimeType);
    const assetHash = scene.assetHash === undefined ? null : String(scene.assetHash);
    if (assetUrl && (!ASSET_MIME_TYPES.has(assetMimeType) || !SAFE_HASH.test(assetHash))) throw new Error('scene asset metadata is invalid');
    return {
      sceneVersionId: scene.sceneVersionId,
      purpose: String(scene.purpose ?? '').slice(0, 2000),
      visualType: safeChoice(scene.visualType, VISUAL_TYPES, 'text'),
      onScreenText: Array.isArray(scene.onScreenText) ? scene.onScreenText.slice(0, 20).map((item) => String(item).slice(0, 500)) : [],
      citationStyle: String(scene.citationStyle ?? '').slice(0, 1000),
      syntheticMediaFlag: Boolean(scene.syntheticMediaFlag),
      assetUrl,
      assetMimeType,
      assetHash,
      durationSeconds: scene.durationSeconds,
    };
  });
  if (durationSeconds > 7200) throw new Error('render duration exceeds limit');
  const safeBrand = {
    name: safeText(brand?.name, 160, 'TubeFactory'),
    logoText: safeText(brand?.logo_text, 12, 'TF'),
    tagline: safeText(brand?.tagline, 160),
    primary: safeColor(brand?.primary, '#174C3C'),
    accent: safeColor(brand?.accent, '#D6A43A'),
    background: safeColor(brand?.background, '#F6F3EA'),
    surface: safeColor(brand?.surface, '#FFFFFF'),
    text: safeColor(brand?.text, '#18201D'),
    mutedText: safeColor(brand?.muted_text, '#56615C'),
    headingFont: safeChoice(brand?.heading_font, FONT_FAMILIES, 'serif'),
    bodyFont: safeChoice(brand?.body_font, FONT_FAMILIES, 'sans'),
    cornerStyle: safeChoice(brand?.corner_style, CORNER_STYLES, 'soft'),
    motionStyle: safeChoice(brand?.motion_style, MOTION_STYLES, 'calm'),
    imageTreatment: safeChoice(brand?.image_treatment, IMAGE_TREATMENTS, 'editorial'),
    visualStyle: safeText(brand?.visual_style, 1000),
    brandHash: SAFE_HASH.test(brand?.brandHash ?? '') ? brand.brandHash : null,
    channelProfileVersion: Number.isInteger(brand?.channelProfileVersion) && brand.channelProfileVersion > 0 ? brand.channelProfileVersion : null,
  };
  return {width, height, fps, durationSeconds, scenes: normalizedScenes, brand: safeBrand};
};
