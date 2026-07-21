import React from 'react';
import {AbsoluteFill, Sequence, interpolate, useCurrentFrame} from 'remotion';

const textShadow = '0 2px 18px rgba(0,0,0,.65)';

const Scene = ({scene, brand, fps}) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, Math.max(1, fps / 3)], [0, 1], {extrapolateRight: 'clamp'});
  const synthetic = Boolean(scene.syntheticMediaFlag);
  return (
    <AbsoluteFill style={{background: `radial-gradient(circle at 70% 25%, ${brand.primary}33, transparent 42%), ${brand.background}`, color: '#f8fafc', fontFamily: 'Arial, sans-serif', padding: '7%', opacity}}>
      <div style={{fontSize: 22, letterSpacing: 3, textTransform: 'uppercase', color: brand.primary}}>{brand.name}</div>
      <div style={{marginTop: 42, maxWidth: '88%', fontSize: 56, lineHeight: 1.08, fontWeight: 760, textShadow}}>{scene.purpose}</div>
      <div style={{marginTop: 30, maxWidth: '78%', fontSize: 28, lineHeight: 1.35, color: '#cbd5e1', textShadow}}>{scene.visualBrief}</div>
      {scene.onScreenText?.length > 0 ? <div style={{marginTop: 28, fontSize: 24, color: '#e2e8f0'}}>{scene.onScreenText.join(' • ')}</div> : null}
      <div style={{position: 'absolute', left: '7%', right: '7%', bottom: '7%', borderTop: `2px solid ${brand.primary}88`, paddingTop: 14, display: 'flex', justifyContent: 'space-between', fontSize: 18, color: '#94a3b8'}}>
        <span>{scene.citationStyle || 'Sources in production manifest'}</span>
        {synthetic ? <span style={{color: '#fbbf24'}}>Synthetic visual</span> : null}
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
