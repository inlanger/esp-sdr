"""Exercise the S3 worker's actual frame emission and final flush on the host."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

class S3EmptySpectrum(unittest.TestCase):
    @unittest.skipUnless(shutil.which('cc'), 'Host C compiler unavailable')
    def test_empty_tail_and_empty_work_preserve_frame_accounting(self):
        src=(ROOT/'main/common/ring_capture.c').read_text()
        encoder=src[src.index('/* Frame being encoded on core 1'):src.index('/* One slice of the pending frame')]
        emit=src[src.index('IRAM_ATTR static void c1_emit_frame('):src.index('/* PIE loads one extra vector')]
        worker=src[src.index('IRAM_ATTR void s3_core1_main('):src.index('static inline void c1_revoke(')]
        finish=worker[worker.index('        if (st.frame_'):worker.index('        c1.done = 1;')]
        harness=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#define IRAM_ATTR
#define MEMW() ((void)0)
#define SPEC_MAGIC 0x31435053u
typedef struct __attribute__((packed)) {
 uint32_t magic,frame;uint64_t pair_index;uint32_t pairs;
 uint16_t ffts;uint8_t flags,gain;uint16_t drops;uint8_t nfft_log2,db_step;
} spec_header_t;
typedef struct {unsigned frames,drops;} ring_result_t;
static ring_result_t result;
static struct {bool max_hold;} cfg;
static struct {
 ring_result_t *res;typeof(cfg)*cfg;bool dropped;
 unsigned frame_units,frame_ffts,frame_pairs,frame_flags,frame_gain;
 uint64_t frame_index;
}st;
static unsigned spec_n,spec_log2,pushed;
static uint32_t accum_buf[2][2048],*accum;
static uint8_t frame_out[2080];
static int32_t log_e_q4[256],log_m_q4[128];
static spec_header_t delivered;
'''
        encoding=r'''
static void c1_encode_step(void) {
 assert(c1enc.pending);memcpy(&delivered,frame_out,sizeof(delivered));
 assert(delivered.ffts>0);pushed++;result.frames++;st.dropped=false;c1enc.pending=0;
}
'''
        checks=r'''
static void setup(unsigned bins,bool maximum) {
 memset(&st,0,sizeof(st));memset(&result,0,sizeof(result));memset(&c1enc,0,sizeof(c1enc));
 cfg.max_hold=maximum;st.cfg=&cfg;st.res=&result;pushed=0;accum=accum_buf[0];
 spec_n=bins;spec_log2=8;while((1u<<spec_log2)<bins)spec_log2++;
 st.frame_units=1;st.frame_pairs=12336;st.frame_index=UINT64_C(9600000000);st.frame_gain=60;
}
int main(void) {
 for(unsigned n=256;n<=2048;n*=2)for(unsigned maximum=0;maximum<2;maximum++) {
  /* A stride-selected tail can span samples without containing an FFT. */
  setup(n,maximum);finish();assert(pushed==0&&result.frames==0&&result.drops==0);
  /* A queued nonempty frame must still drain when the new tail is empty. */
  setup(n,maximum);st.frame_ffts=1;c1_emit_frame();
  st.frame_units=1;st.frame_pairs=12336;finish();
  assert(pushed==1&&result.frames==1&&result.drops==0);
  /* A nonempty partial tail must not be lost. */
  setup(n,maximum);st.frame_ffts=1;finish();
  assert(pushed==1&&delivered.ffts==1&&delivered.pair_index==UINT64_C(9600000000));
  assert(delivered.pairs==12336&&delivered.gain==60&&delivered.nfft_log2==spec_log2);
  assert(delivered.flags==maximum&&delivered.db_step==2&&result.drops==0);
  /* Runtime work abandoned before any FFT remains visible as a drop. */
  setup(n,maximum);st.frame_units=120;st.frame_flags=2;c1_emit_frame();
  assert(!c1enc.pending&&pushed==0&&result.drops==1&&st.dropped);
  assert(!st.frame_units&&!st.frame_pairs&&!st.frame_flags&&accum==accum_buf[0]);
  st.frame_units=1;st.frame_pairs=12336;st.frame_ffts=1;finish();
  assert(pushed==1&&delivered.frame==1&&delivered.drops==1);
  assert(delivered.flags==(maximum|4)&&!st.dropped&&result.frames==1);
 }
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'empty.c'
            path.write_text(harness+encoder+encoding+emit+'static void finish(void){\n'+finish+'}\n'+checks)
            exe=Path(tmp)/'empty'
            subprocess.run(['cc','-std=gnu11','-fsanitize=undefined',str(path),'-o',str(exe)],check=True)
            subprocess.run([str(exe)],check=True,timeout=5)
