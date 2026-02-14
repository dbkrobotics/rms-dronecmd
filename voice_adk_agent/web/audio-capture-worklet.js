class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frameSize = 1600;
    this.buffer = new Int16Array(this.frameSize);
    this.offset = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) {
      return true;
    }

    const channel = input[0];
    for (let i = 0; i < channel.length; i += 1) {
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this.buffer[this.offset] = sample < 0 ? sample * 32768 : sample * 32767;
      this.offset += 1;

      if (this.offset >= this.frameSize) {
        const out = this.buffer.slice(0);
        this.port.postMessage(out.buffer, [out.buffer]);
        this.buffer = new Int16Array(this.frameSize);
        this.offset = 0;
      }
    }

    return true;
  }
}

registerProcessor("pcm-capture-processor", PcmCaptureProcessor);
