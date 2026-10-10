#!/usr/bin/env python3
"""Temporary public-data transfer helper for Cassini Titan SAR.

Reads the full native 46080x23040 USGS SAR/HiSAR mosaic, computes local native-scale
morphology in bounded windows, and area-aggregates to a 1536x768 grid that retains
the source CRS/bounds. No Titan private-repository code is imported.
"""
from pathlib import Path
import json, math
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.windows import Window
from scipy.ndimage import uniform_filter

SRC = Path('sar/Titan_SAR_HiSAR_MosaicThru_T104_Jan2015_clon180_128ppd.tif')
OUT = Path('sar_compact'); OUT.mkdir(exist_ok=True)
TW, TH = 1536, 768
WINDOW_KM = (3.0, 9.0)
BLOCK = 32


def weighted_mean(x, valid, size):
    v = valid.astype(np.float32)
    den = uniform_filter(v, size=size, mode='nearest')
    num = uniform_filter(np.where(valid, x, 0).astype(np.float32), size=size, mode='nearest')
    return np.divide(num, den, out=np.full_like(num, np.nan), where=den > 1e-6)


def local_std(x, valid, size):
    m = weighted_mean(x, valid, size)
    m2 = weighted_mean(x*x, valid, size)
    return np.sqrt(np.maximum(m2-m*m, 0)).astype(np.float32)


def block_mean(a, valid, factor, dh, dw):
    a = a.reshape(dh, factor, dw, factor)
    v = valid.reshape(dh, factor, dw, factor)
    n = v.sum(axis=(1,3))
    s = np.where(v, a, 0).sum(axis=(1,3))
    return np.divide(s, n, out=np.full((dh,dw), np.nan, np.float32), where=n>0).astype(np.float32)


def block_fraction(valid, factor, dh, dw):
    return valid.reshape(dh,factor,dw,factor).mean(axis=(1,3)).astype(np.float32)

with rasterio.open(SRC) as src:
    if src.count != 1:
        raise RuntimeError(f'expected one SAR band, got {src.count}')
    if src.width % TW or src.height % TH:
        raise RuntimeError(f'source shape {src.width}x{src.height} is not an integer multiple of {TW}x{TH}')
    fx, fy = src.width//TW, src.height//TH
    if fx != fy:
        raise RuntimeError(f'non-square aggregation factor {fx}x{fy}')
    factor = fx
    # Published sampling is ~351.11 m/pixel. Prefer transform-derived scale if projected metres.
    if src.crs and src.crs.is_projected:
        pixel_km = (abs(src.transform.a)+abs(src.transform.e))*0.0005
    else:
        pixel_km = 0.35111
    kernels=[]
    for km in WINDOW_KM:
        k=max(3,int(round(km/max(pixel_km,1e-6))))
        if k%2==0: k+=1
        kernels.append(k)
    halo=max(kernels)//2+2
    transform = from_bounds(*src.bounds, TW, TH)
    profile = dict(driver='GTiff',width=TW,height=TH,count=4,dtype='float32',crs=src.crs,transform=transform,nodata=np.nan,compress='deflate',predictor=3)
    lineprof=profile.copy(); lineprof.update(count=3)
    supprof=profile.copy(); supprof.update(count=1)
    tex=OUT/'Titan_SAR_native_compact_texture__scientific.tif'
    lin=OUT/'Titan_SAR_native_compact_lineation__scientific.tif'
    sup=OUT/'Titan_SAR_native_compact_support__scientific.tif'
    with rasterio.open(tex,'w',**profile) as td, rasterio.open(lin,'w',**lineprof) as ld, rasterio.open(sup,'w',**supprof) as sd:
        for i,n in enumerate(['SAR_mosaic_DN','SAR_local_std_3km_DN','SAR_local_std_9km_DN','SAR_gradient_magnitude_DN_per_km'],1): td.set_band_description(i,n)
        for i,n in enumerate(['SAR_lineation_deg_0_180','SAR_lineation_resultant','SAR_lineation_coherence'],1): ld.set_band_description(i,n)
        sd.set_band_description(1,'SAR_valid_area_fraction')
        for tr in range(0,TH,BLOCK):
            dh=min(BLOCK,TH-tr)
            for tc in range(0,TW,BLOCK):
                dw=min(BLOCK,TW-tc)
                r0=tr*factor; c0=tc*factor
                r1=(tr+dh)*factor; c1=(tc+dw)*factor
                rr0=max(0,r0-halo); cc0=max(0,c0-halo); rr1=min(src.height,r1+halo); cc1=min(src.width,c1+halo)
                win=Window(cc0,rr0,cc1-cc0,rr1-rr0)
                ma=src.read(1,window=win,masked=True)
                data=np.asarray(ma.filled(0),np.float32)
                valid=(~np.ma.getmaskarray(ma)) & np.isfinite(data)
                if src.nodata is not None: valid &= data != src.nodata
                # If the file lacks formal nodata, do not infer nodata from darkness: zero may be a real radar-dark surface.
                gy,gx=np.gradient(data)
                grad=np.hypot(gx,gy)/max(pixel_km,1e-6)
                s3=local_std(data,valid,kernels[0])
                s9=local_std(data,valid,kernels[1])
                # Structure tensor at the wider physical scale.
                jxx=weighted_mean(gx*gx,valid,kernels[1]); jyy=weighted_mean(gy*gy,valid,kernels[1]); jxy=weighted_mean(gx*gy,valid,kernels[1])
                delta=np.sqrt((jxx-jyy)**2+4*jxy*jxy)
                coh=np.divide(delta,jxx+jyy+1e-6,out=np.zeros_like(delta),where=np.isfinite(delta))
                ori=(0.5*np.rad2deg(np.arctan2(2*jxy,jxx-jyy)))%180.0
                cos2=coh*np.cos(np.deg2rad(2*ori)); sin2=coh*np.sin(np.deg2rad(2*ori))
                # Crop halo back to exactly factor*target-cell dimensions.
                rs=r0-rr0; cs=c0-cc0; re=rs+dh*factor; ce=cs+dw*factor
                core_valid=valid[rs:re,cs:ce]
                arrays=[data[rs:re,cs:ce],s3[rs:re,cs:ce],s9[rs:re,cs:ce],grad[rs:re,cs:ce]]
                w=Window(tc,tr,dw,dh)
                for bi,a in enumerate(arrays,1): td.write(block_mean(a,core_valid,factor,dh,dw),bi,window=w)
                support=block_fraction(core_valid,factor,dh,dw); sd.write(support,1,window=w)
                c=block_mean(coh[rs:re,cs:ce],core_valid,factor,dh,dw)
                cx=block_mean(cos2[rs:re,cs:ce],core_valid,factor,dh,dw); sy=block_mean(sin2[rs:re,cs:ce],core_valid,factor,dh,dw)
                o=(0.5*np.rad2deg(np.arctan2(sy,cx)))%180.0
                resultant=np.divide(np.hypot(cx,sy),c,out=np.zeros_like(c),where=np.isfinite(c)&(c>1e-8))
                o[support<=0]=np.nan; resultant[support<=0]=np.nan; c[support<=0]=np.nan
                ld.write(o.astype(np.float32),1,window=w); ld.write(resultant.astype(np.float32),2,window=w); ld.write(c.astype(np.float32),3,window=w)
        tags={'source':str(SRC),'processing':'native-window morphology then exact 30x30 area aggregation','source_shape':f'{src.width}x{src.height}','target_shape':f'{TW}x{TH}','native_pixel_km_approx':str(pixel_km),'window_km':json.dumps(WINDOW_KM),'aggregation_factor':str(factor),'zero_dn_policy':'zero retained unless source formally marks it nodata'}
        td.update_tags(**tags); ld.update_tags(**tags); sd.update_tags(**tags)
    meta={'source':str(SRC),'source_shape':[src.width,src.height],'target_shape':[TW,TH],'factor':factor,'pixel_km':pixel_km,'kernels_px':kernels,'source_crs':str(src.crs),'source_bounds':list(src.bounds),'source_nodata':src.nodata,'outputs':[str(tex),str(lin),str(sup)]}
    (OUT/'SAR_compact_manifest.json').write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2))
