from hiss import *
x = sf.read('clips16/ears_p047_f.wav')[0]; x = x[:len(x)//320*320]
PX = np.abs(stft(x))**2
hbx = bands_db(PX, [(4000, 8000)])[:, 0]; midx = bands_db(PX, [(2000, 3900)])[:, 0]; lox = bands_db(PX, [(0, 1000)])[:, 0]
tot = 10*np.log10(PX.sum(1)+1e-12); cons = (hbx - lox > -10) & (tot > np.percentile(tot, 30))
v = vl.SottoV37(); y8 = vl.synthesise(v.decode(v.encode(resample_poly(x, 1, 2))))[160:]
outs = {'v37': resample_poly(np.pad(y8, (0, len(x)//2 - len(y8)))[:len(x)//2], 2, 1)}
for n, br, mh in [('hiss0', 0, 'g'), ('hiss7', 0, None), ('hiss7b', 12, None), ('hiss7bm', 12, -3.0)]:
    h = Hiss('hiss_model.npz', burst=br); b = h.encode(x)
    outs[n] = h.decode(b, mode='hiss0' if mh == 'g' else 'hiss7', n16=len(x), mixed_hb=None if mh == 'g' else mh)
    if n == 'hiss7': code = b[:, 0]*2 + b[:, 1]; d = h._dstruct(b, h._decode_model(b), len(x))
nv=len(d['v']); tot=tot[:nv]
for cls,ci in (('U',0),('M',2)):
    cons=(d['v']==ci)&(tot>np.percentile(tot,30))
    print(cls,'frames',cons.sum())
    for n,y in outs.items():
        P=np.abs(stft(y[:len(x)]))**2; a=bands_db(P,[(4000,8000)])[:nv,0]; b2=bands_db(P,[(2000,3900)])[:nv,0]
        print(f'  {n:8s} 2-3.9k {np.mean(b2[cons]-midx[:nv][cons]):+.1f}  4-8k {np.mean(a[cons]-hbx[:nv][cons]):+.1f}')
import sys; sys.exit()
print('consonant frames', cons.sum(), ' codec class of them:', {c: int(np.sum(d['v'][cons[:len(d['v'])]] == i)) for i, c in enumerate('UVM')})
for n, y in outs.items():
    P = np.abs(stft(y[:len(x)]))**2
    print(f'{n:8s} 2-3.9k diff {np.mean(bands_db(P,[(2000,3900)])[:,0][cons]-midx[cons]):+.1f} dB   4-8k diff {np.mean(bands_db(P,[(4000,8000)])[:,0][cons]-hbx[cons]):+.1f} dB')
