const fs = require('node:fs');
const vm = require('node:vm');
let Processor;
let sent = 0;
const context = {Float32Array, Int16Array, sampleRate:48000,
 AudioWorkletProcessor:class { constructor() { this.port={postMessage:buffer=>{sent+=buffer.byteLength/2;}}; } },
 registerProcessor:(name,value)=>{if(name==='mic-processor')Processor=value;}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('app/static/js/audio-processor.js','utf8'),context);
const processor=new Processor();
let accepted=0;
for(let i=0;i<375;i++) {
 const input=new Float32Array(128).fill(0.25);
 accepted+=input.length;
 processor.process([[input]]);
}
const report={probe:'Production AudioWorklet sample count for one second of 48kHz input in 128-sample blocks',
 source:'app/static/js/audio-processor.js',input_samples:accepted,input_rate:48000,output_rate:16000,
 total_generated_samples:sent+processor._bufferLength,sent_samples:sent,buffered_samples:processor._bufferLength,
 nominal_output_samples:16000,delta_samples:sent+processor._bufferLength-16000,
 scope:'Offline Node VM with only AudioWorkletProcessor/registerProcessor boundary doubles; no source edits or devices'};
fs.writeFileSync(process.argv[2],JSON.stringify(report,null,2)+'\n','utf8');
process.stdout.write(JSON.stringify(report)+'\n');
