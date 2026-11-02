from hiss import *
o = sf.read('ab12/audio/t10o.wav')[0]; A = sf.read('ab12/audio/t10a.wav')[0]; B = sf.read('ab12/audio/t10b.wav')[0]
BANDS = [(0,1000),(1000,2000),(2000,3000),(3000,4000),(4000,6000),(6000,8000)]
po, pa, pb = [bands_db(np.abs(stft(y))**2, BANDS) for y in (o, A, B)]
x = sf.read('clips16/ears_p047_f.wav')[0]; x = x[:len(x)//320*320]
h = Hiss('hiss_model.npz', burst=12); b = h.encode(x); m = h._decode_model(b); d = h._dstruct(b, m, len(x))
code = b[:,0]*2 + b[:,1]
print(' t(s) | orig 0-1k 1-2 2-3 3-4 4-6 6-8 | A-orig ... | B-orig | v  frame code f0')
for t in range(292, 322):
    ts = t*0.01 + 0.01
    f0 = m['Wo'][t+2]*4000/np.pi if m['voiced'][t+2] else 0
    print(f'{ts:.2f} | ' + ' '.join(f'{v:4.0f}' for v in po[t]) + ' | ' + ' '.join(f'{v:+4.0f}' for v in pa[t]-po[t]) + ' | ' + ' '.join(f'{v:+4.0f}' for v in pb[t]-po[t]) + f' | {d["v"][t]} {"UVME"[code[d["frame"][t]]]} {f0:.0f}')
