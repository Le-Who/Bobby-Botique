/**
 * AudioWorklet — captures mic audio, resamples to 16kHz mono PCM16,
 * and posts ArrayBuffers to the main thread for WebSocket transmission.
 *
 * Registered as 'mic-processor' by the Live Audio Mini App.
 */

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = [];
    this._bufferLength = 0;
    this._inputSamples = 0;
    this._outputSamples = 0;
    this._lastSample = 0;
    // We'll send chunks every ~100ms of audio at 16kHz = 1600 samples
    this._chunkSize = 1600;
  }

  /**
   * Downsample from browser native rate (usually 48kHz) to 16kHz
   * using simple linear interpolation.
   */
  _downsample(float32Data, inputRate, outputRate) {
    const start = this._inputSamples;
    const end = start + float32Data.length - 1;
    const result = [];
    while (true) {
      // Use the continuous input timeline, not a rounded length per render
      // quantum. Fractional boundary samples wait for the next input block.
      const position = this._outputSamples * inputRate / outputRate;
      const lo = Math.floor(position);
      const frac = position - lo;
      if (lo > end || (frac > 0 && lo === end)) break;
      const index = lo - start;
      const left = index < 0 ? this._lastSample : float32Data[index];
      const right = frac > 0 ? float32Data[index + 1] : left;
      result.push(left * (1 - frac) + right * frac);
      this._outputSamples++;
    }
    this._inputSamples += float32Data.length;
    this._lastSample = float32Data[float32Data.length - 1];
    return Float32Array.from(result);
  }

  /**
   * Convert Float32 [-1.0, 1.0] to Int16 PCM [-32768, 32767].
   */
  _float32ToInt16(float32Data) {
    const pcm16 = new Int16Array(float32Data.length);
    for (let i = 0; i < float32Data.length; i++) {
      const s = Math.max(-1, Math.min(1, float32Data[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return pcm16;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;

    const channelData = input[0]; // mono — first channel
    if (!channelData || channelData.length === 0) return true;

    // Downsample to 16kHz (sampleRate is set by the AudioContext)
    const downsampled = this._downsample(channelData, sampleRate, 16000);

    // Accumulate until we have a full chunk
    this._buffer.push(downsampled);
    this._bufferLength += downsampled.length;

    if (this._bufferLength >= this._chunkSize) {
      // Merge accumulated buffers
      const merged = new Float32Array(this._bufferLength);
      let offset = 0;
      for (const buf of this._buffer) {
        merged.set(buf, offset);
        offset += buf.length;
      }
      this._buffer = [];
      this._bufferLength = 0;

      // Convert to Int16 PCM and send to main thread
      const pcm16 = this._float32ToInt16(merged);
      this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    }

    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
