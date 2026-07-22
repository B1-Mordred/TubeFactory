import React from 'react';
import {Composition} from 'remotion';
import {EvidenceVideo} from './video.jsx';

const defaults = {
  width: 854,
  height: 480,
  fps: 24,
  durationSeconds: 1,
  scenes: [],
  brand: {name: 'TubeFactory', logoText: 'TF', primary: '#174C3C', accent: '#D6A43A', background: '#F6F3EA', surface: '#FFFFFF', text: '#18201D', mutedText: '#56615C', headingFont: 'serif', bodyFont: 'sans', cornerStyle: 'soft', motionStyle: 'calm', imageTreatment: 'editorial'},
};

export const RemotionRoot = () => (
  <Composition
    id="EvidenceVideo"
    component={EvidenceVideo}
    width={defaults.width}
    height={defaults.height}
    fps={defaults.fps}
    durationInFrames={defaults.fps}
    defaultProps={defaults}
    calculateMetadata={({props}) => ({
      width: props.width,
      height: props.height,
      fps: props.fps,
      durationInFrames: Math.max(1, Math.ceil(props.durationSeconds * props.fps)),
      props,
    })}
  />
);
