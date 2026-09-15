import pickle
import numpy as np

M5 = pickle.load(open('_sp2l_data_5y.pkl', 'rb'))[0]
sp = M5['spread']
print('n =', len(sp))
print('mean =', round(sp.mean(), 1), 'pts ($' + format(sp.mean() * 0.01, '.2f') + ')')
for q in (25, 50, 75, 90, 95, 99):
    v = float(np.percentile(sp, q))
    print('P' + str(q) + ' = ' + format(v, '.0f') + ' pts ($' + format(v * 0.01, '.2f') + ')')
print('min/max =', sp.min(), '/', sp.max())
M5s = M5.copy()
M5s['year'] = M5.index.year
print(M5s.groupby('year')['spread'].agg(['median', 'mean']).round(1))
