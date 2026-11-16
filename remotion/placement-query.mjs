// Pure package placement bridge. Input and output are JSON on standard streams.
import {intrinsicMediaSize, normalizeClipPlacementWithFindings, resolveClipPlacement} from '@banodoco/timeline-composition';
let input = '';
for await (const chunk of process.stdin) input += chunk;
try {
  const request = JSON.parse(input);
  const answer = request.operation === 'normalize'
    ? normalizeClipPlacementWithFindings(request.config)
    : request.operation === 'resolve'
      ? resolveClipPlacement({...request.input, intrinsic: request.asset === undefined ? request.input.intrinsic : (() => {
          const intrinsic = intrinsicMediaSize(request.asset);
          if (![intrinsic.width, intrinsic.height].every(value => Number.isFinite(value) && value > 0)) {
            throw new Error(`clip '${request.input.clip?.id}' asset '${request.input.clip?.asset}' property resolution is missing or invalid; record the media resolution`);
          }
          return intrinsic;
        })()})
      : (() => { throw new Error('unknown placement operation'); })();
  process.stdout.write(JSON.stringify(answer));
} catch (error) {
  process.stderr.write(String(error?.message ?? error));
  process.exitCode = 1;
}
