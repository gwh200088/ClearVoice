# -*- coding: utf-8 -*-
"""
Created on Wed Jun 26 15:54:05 2019

@author: chkarada
"""
import soundfile as sf
import os
import numpy as np

# Function to read audio
def audioread(path, norm = True, start=0, stop=None):
    path = os.path.abspath(path)
    if not os.path.exists(path):
        #raise ValueError("[{}] does not exist!".format(path))
        return None, None
    try:
        x, sr = sf.read(path, start=start, stop=stop)
    except RuntimeError:  # fix for sph pcm-embedded shortened v2
        print('WARNING: Audio type not supported')
        return None, None
    if np.isnan(x).any() or np.isinf(x).any():
        return None, None

    if len(x.shape) == 1:  # mono
        if norm:
            rms = (x ** 2).mean() ** 0.5
            if rms == 0: return None, None
            scalar = 10 ** (-25 / 20) / (rms)            
            x = x * scalar
            if np.isnan(x).any() or np.isinf(x).any():
                return None, None
        return x, sr
    else:  # multi-channel
        x = x.T
        x = x.sum(axis=0)/x.shape[0]
        if norm:
            rms = (x ** 2).mean() ** 0.5
            if rms == 0: return None, None
            scalar = 10 ** (-25 / 20) / (rms)
            x = x * scalar
            if np.isnan(x).any() or np.isinf(x).any():
                return None, None
        return x, sr
    
# Funtion to write audio    
def audiowrite(data, fs, destpath, norm=False):
    if norm:
        rms = (data ** 2).mean() ** 0.5
        scalar = 10 ** (-25 / 10) / (rms+eps)
        data = data * scalar
        if max(abs(data))>=1:
            data = data/max(abs(data), eps)
    
    destpath = os.path.abspath(destpath)
    destdir = os.path.dirname(destpath)
    
    if not os.path.exists(destdir):
        os.makedirs(destdir)
    
    sf.write(destpath, data, fs)
    return

# Function to mix clean speech and noise at various SNR levels
def snr_mixer(clean, noise, snr):
    # Normalizing to -25 dB FS
    rmsclean = (clean**2).mean()**0.5
    scalarclean = 10 ** (-25 / 20) / rmsclean
    clean = clean * scalarclean
    rmsclean = (clean**2).mean()**0.5

    rmsnoise = (noise**2).mean()**0.5
    scalarnoise = 10 ** (-25 / 20) /rmsnoise
    noise = noise * scalarnoise
    rmsnoise = (noise**2).mean()**0.5
    
    # Set the noise level for a given SNR
    noisescalar = np.sqrt(rmsclean / (10**(snr/20)) / rmsnoise)
    noisenewlevel = noise * noisescalar
    noisyspeech = clean + noisenewlevel
    return clean, noisenewlevel, noisyspeech
      
def snr_mixer_weak_voice(clean, noise, snr, fs):
    # Normalizing to -25 dB FS
    
    pow_clean = clean**2
    avg_pow_clean = pow_clean.mean()
    rmsclean = pow_clean[pow_clean>avg_pow_clean].mean()**0.5

    #rmsclean = (clean**2).mean()**0.5
    scalarclean = 10 ** (-25 / 20) / rmsclean
    clean = clean * scalarclean
    rmsclean = (clean**2).mean()**0.5

    pow_noise = noise**2
    avg_pow_noise = pow_noise.mean()
    rmsnoise = pow_noise[pow_noise>avg_pow_noise].mean()**0.5

    #rmsnoise = (noise**2).mean()**0.5
    scalarnoise = 10 ** (-25 / 20) /rmsnoise
    noise = noise * scalarnoise
    rmsnoise = (noise**2).mean()**0.5

    # Set the noise level for a given SNR
    noisescalar = np.sqrt(rmsclean / (10**(snr/20)) / rmsnoise)
    noisenewlevel = noise * noisescalar
    sig_len = len(clean)
    if sig_len > fs*1.0 and int(np.random.uniform(low=0.0, high=10.0)) > 3:
        dur = int(np.random.uniform(low=fs*0.1, high=fs*1.0))
        st = int(np.random.uniform(low=0.0, high=sig_len - dur))
        ed = st + dur
        if int(np.random.uniform(low=0.0, high=10.0)) > 3:
            clean[st:ed] = clean[st:ed]  * (np.random.randint(9,size=1)+1) * 0.1
            noisenewlevel[st:ed] = noisenewlevel[st:ed] * (np.random.randint(9,size=1)+1) * 0.01
        else:
            clean[st:ed] = clean[st:ed]  * (np.random.randint(9,size=1)+1) * 0.1    
    noisyspeech = clean + noisenewlevel
    return clean, noisenewlevel, noisyspeech

def snr_mixer_no_scale(clean, noise, snr):
    # Normalizing to -25 dB FS

    # Set the noise level for a given SNR
    #noisescalar = np.sqrt(rmsclean / (10**(snr/20)) / rmsnoise)
    noisenewlevel = noise #* noisescalar
    noisyspeech = clean + noisenewlevel
    return clean, noisenewlevel, noisyspeech
 
# read filename list from scp file
def get_filenames(input_file):
    filenames = []
    f = open(input_file, 'r')
    while 1:
        line = f.readline().strip()
        #print(line)
        if not line: break
        if len(line.split('\t')) > 1:
            name, path = line.split('\t')
        else:
            path = line
        filenames.append(path)
    return filenames
