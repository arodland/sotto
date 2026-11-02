from hiss import *
D = '/mnt/project-files/vocoder/listening/act'; os.makedirs(D, exist_ok=True)
x = sf.read('clips16/ears_p047_f.wav')[0]; x = x[:len(x)//320*320]
a, b_ = int(2.5*16000), int(4.0*16000)
def snip(y):
    y = y[a:b_].copy(); f = 160; y[:f] *= np.linspace(0,1,f); y[-f:] *= np.linspace(1,0,f)
    return y * np.sqrt(np.mean(x[a:b_]**2) / np.mean(y**2))
sf.write(f'{D}/act_0_original.wav', snip(x), 16000, subtype='PCM_16')
for i, (n, fx) in enumerate([('round12_B', False), ('gated_boost', 'gate'), ('gated_boost_plus_onset_fix', 'onset')], 1):
    h = Hiss('hiss_model.npz', burst=12, fix2=fx); y = h.decode(h.encode(x), mode='hiss7', n16=len(x), mixed_hb=-3.0)
    sf.write(f'{D}/act_{i}_{n}.wav', snip(y), 16000, subtype='PCM_16')
print(sorted(os.listdir(D)))
