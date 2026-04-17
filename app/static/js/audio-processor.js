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
    // We'll send chunks every ~100ms of audio at 16kHz = 1600 samples
    this._chunkSize = 1600;
  }

  /**
   * Downsample from browser native rate (usually 48kHz) to 16kHz
   * using simple linear interpolation.
   */
  _downsample(float32Data, inputRate, outputRate) {
    if (inputRate === outputRate) return float32Data;
    const ratio = inputRate / outputRate;
    const newLen = Math.round(float32Data.length / ratio);
    const result = new Float32Array(newLen);
    for (let i = 0; i < newLen; i++) {
      const srcIdx = i * ratio;
      const lo = Math.floor(srcIdx);
      const hi = Math.min(lo + 1, float32Data.length - 1);
      const frac = srcIdx - lo;
      result[i] = float32Data[lo] * (1 - frac) + float32Data[hi] * frac;
    }
    return result;
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
