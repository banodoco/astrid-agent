import {describe, expect, it} from 'vitest';
import {sourceSegmentsAtFps, sourceSegmentAt, transformAt, validateKeyframes} from '../../../astrid/packs/local/rendering/elements/effects/animated-media-transform/motion';
import {endSpanningTiming, selectedSegmentIndex, validSegments} from '../../../astrid/packs/local/rendering/elements/effects/end-spanning-layer/timing';

describe('retained Personal motion and timing',()=>{
  it('interpolates position/size/opacity and clamps before and after authored samples',()=>{
    const keys=validateKeyframes([{at:0,x:0,y:20,width:100,height:50,opacity:0},{at:2,x:80,y:0,width:200,height:100,opacity:1}]);
    expect(transformAt(keys,1)).toMatchObject({x:40,y:10,width:150,height:75,opacity:0.5});
    expect(transformAt(keys,-1)).toEqual(keys[0]);expect(transformAt(keys,3)).toEqual(keys[1]);
    expect(()=>validateKeyframes([{...keys[0],width:0}])).toThrow();
  });
  it('partitions source resets at rounded frame boundaries with no gap or overlap',()=>{
    const segments=sourceSegmentsAtFps([{at:0,sourceStart:10,speed:1},{at:0.51,sourceStart:0,speed:2}],24,48);
    expect(segments.map(s=>[s.fromFrame,s.durationInFrames])).toEqual([[0,12],[12,36]]);
    expect(sourceSegmentAt(segments,11)?.sourceStart).toBe(10);
    expect(sourceSegmentAt(segments,12)?.sourceStart).toBe(0);
    expect(()=>sourceSegmentsAtFps([{at:0,sourceStart:0,speed:1},{at:0.001,sourceStart:4,speed:1}],24,48)).toThrow('distinct increasing frames');
  });
  it('keeps cumulative phase boundaries on the authored frame clock and selects the requested source',()=>{
    const params={phaseDurations:{prep:0.51,iteration:0.51,anchors:0.51,workflow:0.51},selectedSegmentId:'blue'};
    expect(endSpanningTiming({at:0,hold:3},params,24)).toEqual({clipSeconds:3,seconds:[0.51,0.51,0.51,0.51],frames:[12,24,37,49],effectEndFrame:72});
    const segments=validSegments(null);
    expect(segments[selectedSegmentIndex(params,segments)].id).toBe('blue');
    expect(selectedSegmentIndex({selectedSegmentIndex:999},segments)).toBe(segments.length-1);
  });
});
