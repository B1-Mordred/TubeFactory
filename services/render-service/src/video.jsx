import React from 'react';
import {AbsoluteFill, Img, OffthreadVideo, Sequence, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';

const textShadow = '0 2px 18px rgba(0,0,0,.65)';
const fonts = {
  sans: 'Arial, Helvetica, sans-serif',
  serif: 'Georgia, Times New Roman, serif',
  rounded: 'Arial Rounded MT Bold, Trebuchet MS, sans-serif',
  mono: 'Liberation Mono, Courier New, monospace',
};
const radii = {square: 0, soft: 18, rounded: 38};

const Scene = ({scene, brand, fps}) => {
  const frame = useCurrentFrame();
  const {width, height} = useVideoConfig();
  const opacity = interpolate(frame, [0, Math.max(1, fps / 3)], [0, 1], {extrapolateRight: 'clamp'});
  const scaleEnd = brand.motionStyle === 'dynamic' ? 1.08 : brand.motionStyle === 'calm' ? 1.035 : 1;
  const scale = interpolate(frame, [0, Math.max(1, scene.durationSeconds * fps)], [1, scaleEnd], {extrapolateRight: 'clamp'});
  const synthetic = Boolean(scene.syntheticMediaFlag);
  const radius = radii[brand.cornerStyle] ?? radii.soft;
  const video = scene.assetUrl && scene.assetMimeType?.startsWith('video/');
  const image = scene.assetUrl && scene.assetMimeType?.startsWith('image/');
  const trimStartFrames = Math.max(0, Math.round((scene.trimStartSeconds || 0) * fps));
  const trimEndFrames = scene.trimEndSeconds ? Math.max(trimStartFrames + 1, Math.round(scene.trimEndSeconds * fps)) : undefined;
  const overlay = brand.imageTreatment === 'cinematic' ? '.72' : brand.imageTreatment === 'documentary' ? '.58' : brand.imageTreatment === 'clean' ? '.32' : '.48';
  return (
    <AbsoluteFill style={{background: brand.background, color: brand.text, fontFamily: fonts[brand.bodyFont], opacity, overflow: 'hidden'}}>
      {image ? <Img src={scene.assetUrl} style={{position: 'absolute', inset: '-2%', width: '104%', height: '104%', objectFit: 'cover', transform: `scale(${scale})`}} /> : null}
      {video ? <OffthreadVideo src={scene.assetUrl} muted startFrom={trimStartFrames} endAt={trimEndFrames} style={{position: 'absolute', inset: 0, width, height, objectFit: 'cover'}} /> : null}
      {!image && !video ? <AbsoluteFill style={{background: `radial-gradient(circle at 76% 20%, ${brand.accent}88, transparent 34%), linear-gradient(135deg, ${brand.background}, ${brand.primary}55)`}} /> : null}
      <AbsoluteFill style={{background: `linear-gradient(90deg, ${brand.background}F2 0%, ${brand.background}${Math.round(Number(overlay) * 255).toString(16).padStart(2, '0')} 48%, transparent 82%)`}} />
      <div style={{position: 'absolute', inset: '6%', display: 'flex', flexDirection: 'column'}}>
        <div style={{display: 'flex', alignItems: 'center', gap: 18}}>
          <div style={{display: 'grid', placeItems: 'center', minWidth: 54, height: 54, padding: '0 12px', borderRadius: radius / 2, background: brand.primary, color: brand.surface, fontWeight: 900, letterSpacing: 1}}>{brand.logoText}</div>
          <div><div style={{fontSize: 22, letterSpacing: 3, textTransform: 'uppercase', color: brand.text, fontWeight: 800}}>{brand.name}</div>{brand.tagline ? <div style={{marginTop: 4, fontSize: 16, color: brand.mutedText}}>{brand.tagline}</div> : null}</div>
        </div>
        <div style={{marginTop: 'auto', maxWidth: '72%', padding: '30px 34px', borderLeft: `7px solid ${brand.accent}`, borderRadius: radius, background: `${brand.surface}E8`, boxShadow: '0 22px 70px rgba(0,0,0,.18)'}}>
          <div style={{fontFamily: fonts[brand.headingFont], fontSize: 52, lineHeight: 1.08, fontWeight: 760, color: brand.text}}>{scene.purpose}</div>
          {scene.onScreenText?.length > 0 ? <div style={{marginTop: 22, fontSize: 24, lineHeight: 1.35, color: brand.mutedText}}>{scene.onScreenText.join(' • ')}</div> : null}
        </div>
        <div style={{display: 'flex', justifyContent: 'space-between', gap: 24, marginTop: 24, paddingTop: 14, borderTop: `2px solid ${brand.primary}88`, fontSize: 16, color: brand.mutedText, textShadow}}>
          <span>{scene.citationStyle || 'Sources in Production Manifest'}</span>
          {synthetic ? <span style={{color: brand.text, fontWeight: 750}}>Synthetic visual</span> : null}
        </div>
      </div>
    </AbsoluteFill>
  );
};

export const EvidenceVideo = ({scenes, brand, fps}) => {
  let cursor = 0;
  return (
    <AbsoluteFill style={{backgroundColor: brand.background}}>
      {scenes.map((scene) => {
        const duration = Math.max(1, Math.round(scene.durationSeconds * fps));
        const from = cursor;
        cursor += duration;
        return <Sequence key={scene.sceneVersionId} from={from} durationInFrames={duration}><Scene scene={scene} brand={brand} fps={fps}/></Sequence>;
      })}
    </AbsoluteFill>
  );
};
