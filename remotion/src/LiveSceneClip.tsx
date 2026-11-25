import {useCallback, useEffect, useMemo} from 'react';
import {cancelRender, continueRender, delayRender} from 'remotion';
import {SceneSurface} from '@astrid-rendering/ui/threejs-live-editor/SceneSurface';
import type {SceneStatus} from '@astrid-rendering/ui/threejs-live-editor/runtime';

/** Uses the existing Remotion frame gate and capture; no second export clock. */
export function LiveSceneClip({source, sourceTime, width, height}: {
  source: unknown; sourceTime: number; width: number; height: number;
}) {
  const gate = useMemo(() => delayRender(`Live scene ${sourceTime}s`, {timeoutInMilliseconds: 35000}), [source, sourceTime, width, height]);
  useEffect(() => () => continueRender(gate), [gate]);
  const onStatus = useCallback((status: SceneStatus) => {
    if (status.phase === 'ready' && status.sourceTime === sourceTime) continueRender(gate);
    if (status.phase === 'error') cancelRender(new Error(status.error));
  }, [gate, sourceTime]);
  return <SceneSurface source={source} sourceTime={sourceTime} width={width} height={height} showStatus={false} onStatus={onStatus} />;
}
