const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const posted = [];
let postedChunkCount = 0;
let Processor;
let transferListsCorrect = true;
const context = {
  Float32Array, Int16Array, sampleRate: request.rate,
  AudioWorkletProcessor: class {
    constructor() {
      this.port = { postMessage(buffer, transfers) {
        transferListsCorrect &&= transfers.length === 1 && transfers[0] === buffer;
        postedChunkCount++;
        posted.push(...new Int16Array(buffer));
      } };
    }
  },
  registerProcessor(name, implementation) {
    assert.equal(name, 'mic-processor');
    Processor = implementation;
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('app/static/js/audio-processor.js', 'utf8'), context);
const processor = new Processor();
let inputSamples = 0;
let block = 0;
let emptyInputChangedState = false;
while (inputSamples < request.rate) {
  if (request.emptyInputs) {
    const before = posted.length + processor._bufferLength;
    for (const empty of [[], [[]], [[new Float32Array(0)]]]) {
      assert.equal(processor.process(empty), true);
    }
    emptyInputChangedState ||= posted.length + processor._bufferLength !== before;
  }
  const length = Math.min(request.blocks[block++ % request.blocks.length], request.rate - inputSamples);
  // A continuous ramp has an independent, exact interpolation oracle: at output
  // time k/16000 seconds its amplitude is -.4 + .8*k/16000, for every input rate.
  const channel = Float32Array.from({ length }, (_, i) => -.4 + .8 * (inputSamples + i) / request.rate);
  assert.equal(processor.process([[channel, new Float32Array(length).fill(1)]]), true);
  inputSamples += length;
}
const pcm = [...posted, ...processor._float32ToInt16(Float32Array.from(processor._buffer.flatMap(b => [...b])))];
let maximumPcmError = 0;
for (let k = 0; k < pcm.length; k++) {
  const expected = -.4 + .8 * k / 16000;
  const expectedPcm = Math.trunc(expected * (expected < 0 ? 32768 : 32767));
  maximumPcmError = Math.max(maximumPcmError, Math.abs(pcm[k] - expectedPcm));
}
const clipping = [...processor._float32ToInt16(Float32Array.from([-2, -1, -.5, 0, .5, 1, 2]))];
process.stdout.write(JSON.stringify({
  inputSamples, samples: pcm.length, maximumPcmError,
  postedChunks: postedChunkCount, transferListsCorrect, emptyInputChangedState,
  clippedPcm: clipping, secondChannelIgnored: maximumPcmError <= 1,
}));
