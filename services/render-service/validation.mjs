const SAFE_COLOR = /^#[0-9a-fA-F]{6}$/;

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
    return {
      sceneVersionId: scene.sceneVersionId,
      purpose: String(scene.purpose ?? '').slice(0, 2000),
      visualBrief: String(scene.visualBrief ?? '').slice(0, 4000),
      onScreenText: Array.isArray(scene.onScreenText) ? scene.onScreenText.slice(0, 20).map((item) => String(item).slice(0, 500)) : [],
      citationStyle: String(scene.citationStyle ?? '').slice(0, 1000),
      syntheticMediaFlag: Boolean(scene.syntheticMediaFlag),
      durationSeconds: scene.durationSeconds,
    };
  });
  if (durationSeconds > 7200) throw new Error('render duration exceeds limit');
  const safeBrand = {
    name: String(brand?.name ?? 'TubeFactory').slice(0, 160),
    primary: SAFE_COLOR.test(brand?.primary ?? '') ? brand.primary : '#6ee7ff',
    background: SAFE_COLOR.test(brand?.background ?? '') ? brand.background : '#08111f',
  };
  return {width, height, fps, durationSeconds, scenes: normalizedScenes, brand: safeBrand};
};
