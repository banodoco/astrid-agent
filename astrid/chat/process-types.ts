export interface JsonRpcReadableStreamLike {
  on(event: 'data', listener: (chunk: string | Uint8Array) => void): unknown;
  on(event: 'close' | 'end', listener: () => void): unknown;
  off?(event: 'data', listener: (chunk: string | Uint8Array) => void): unknown;
  off?(event: 'close' | 'end', listener: () => void): unknown;
  removeListener?(event: 'data', listener: (chunk: string | Uint8Array) => void): unknown;
  removeListener?(event: 'close' | 'end', listener: () => void): unknown;
  setEncoding?(encoding: BufferEncoding): unknown;
}

export interface JsonRpcWritableStreamLike {
  write(
    chunk: string,
    encoding?: BufferEncoding,
    callback?: (error?: Error | null) => void,
  ): boolean;
  end?(): void;
}

export interface JsonRpcProcessLike {
  readonly stdin: JsonRpcWritableStreamLike | null;
  readonly stdout: JsonRpcReadableStreamLike | null;
  readonly pid?: number;
  on(event: 'exit', listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
  on(event: 'error', listener: (error: Error) => void): unknown;
  off?(event: 'exit', listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
  off?(event: 'error', listener: (error: Error) => void): unknown;
  removeListener?(event: 'exit', listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
  removeListener?(event: 'error', listener: (error: Error) => void): unknown;
}

