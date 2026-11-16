// Pure package placement bridge. Input and output are JSON on standard streams.
import {normalizeClipPlacementWithFindings, resolveClipPlacement} from '@banodoco/timeline-composition';
let input = '';
for await (const chunk of process.stdin) input += chunk;
try {
  const request = JSON.parse(input);
  const answer = request.operation === 'normalize'
    ? normalizeClipPlacementWithFindings(request.config)
    : request.operation === 'resolve'
      ? resolveClipPlacement(request.input)
      : (() => { throw new Error('unknown placement operation'); })();
  process.stdout.write(JSON.stringify(answer));
} catch (error) {
  process.stderr.write(String(error?.message ?? error));
  process.exitCode = 1;
}
