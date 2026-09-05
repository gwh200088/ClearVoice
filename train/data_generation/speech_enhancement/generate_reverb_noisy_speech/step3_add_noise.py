"""
This piece of program is modified based on MS-SNSD software
"""
import glob
import numpy as np
import soundfile as sf
import os
import argparse
import configparser as CP
from audiolib import audioread, audiowrite, snr_mixer, get_filenames, snr_mixer_weak_voice, get_filenames_ns
import librosa

def main(cfg, args):
    snr_lower = float(cfg["snr_lower"])
    snr_upper = float(cfg["snr_upper"])
    total_snrlevels = float(cfg["total_snrlevels"])
    
    if cfg["noise_list"]!='None':
        noise_list = cfg["noise_list"]

    curr_run = 'run'+args.run_num
    clean_list = args.output_path+'/'+curr_run+'/'+args.output_reverb_dir+'/wav.lst'

    target_fs = args.sample_rate
    audioformat = cfg["audioformat"]
    total_hours = float(cfg["total_hours"])
    min_audio_length = float(cfg["min_audio_length"])
    max_audio_length = float(cfg["max_audio_length"])
    silence_length = float(cfg["silence_length"])
    Test_flag = cfg["test"]=="True"
    print('Test_flag: {}'.format(Test_flag))

    Random_SNR_flag = cfg["random_snr"]=="True"
    print('Random_SNR_flag: {}'.format(Random_SNR_flag))
    suffix = cfg["suffix"]
    if cfg["save_noise"]!='None':
        Save_noise = cfg["save_noise"]=="True"
    in_audio_root = args.output_path+'/'+curr_run+'/'+args.output_reverb_dir 
    out_audio_root = args.output_path+'/'+curr_run+'/'+args.output_reverb_noisy_dir

    noisyspeech_dir = os.path.join(out_audio_root, 'noisy')
    if not os.path.exists(noisyspeech_dir):
        os.makedirs(noisyspeech_dir)
    clean_proc_dir = os.path.join(out_audio_root, 'target')
    if not os.path.exists(clean_proc_dir):
        os.makedirs(clean_proc_dir)
    noise_proc_dir = os.path.join(out_audio_root, 'noise')
    if not os.path.exists(noise_proc_dir) and Save_noise:
        os.makedirs(noise_proc_dir)
    
    f = open(out_audio_root + '/wav.lst', 'w')    
    total_secs = total_hours*60*60
    total_samples = int(total_secs * target_fs)
    min_audio_length = int(min_audio_length*target_fs)
    max_audio_length = int(max_audio_length*target_fs)
    SNR = np.linspace(snr_lower, snr_upper, int(total_snrlevels))
    SNR = SNR.tolist()

    cleanfilenames = get_filenames(clean_list)
    noisefilenames = get_filenames_ns(noise_list) 
    filecounter = 0
    num_samples = 0
    print('total samples: {}'.format(total_samples))
    count = 0    
    #while num_samples < total_samples:
    total_files = len(cleanfilenames)
    while filecounter < total_files:
        clean_files = []
        noise_files = []
        print('current samples: {}'.format(num_samples))
        idx_s = filecounter
        cleanfilename = cleanfilenames[filecounter]
        clean, fs = audioread(in_audio_root + '/reverb/'+cleanfilename)       
        if clean is None: # or len(clean)/fs < 3:
            filecounter = filecounter + 1
            continue
        if fs != target_fs:
            clean = librosa.resample(clean, fs, target_fs)
        clean_files.append(cleanfilename)
        target_filename = in_audio_root+'/target/'+cleanfilename
        print('target_filename: {}'.format(target_filename))
        target, fs = audioread(target_filename)
        if fs != target_fs:
            target = librosa.resample(target, fs, target_fs)
        len_clean = len(clean)

        if Test_flag:
            idx_n = count
            while idx_n >= len(noisefilenames):
                idx_n = idx_n - len(noisefilenames)
        else:
            idx_n = np.random.randint(0, np.size(noisefilenames))
        noise, fs = audioread(noisefilenames[idx_n])
        print(noisefilenames[idx_n])
        while noise is None: # or len(noise)/fs < 3:
            idx_n = np.random.randint(0, np.size(noisefilenames))
            noise, fs = audioread(noisefilenames[idx_n])
        if fs != target_fs:
            noise = librosa.resample(noise, fs, target_fs)
        noisefilename = noisefilenames[idx_n].split("/")[-1]
        noisefilename = noisefilename.split(".")[0]
        noise_files.append(noisefilename)        
        len_noise = len(noise)
        len_clean = len(clean)
        print('len_clean: {}, len_noise: {}'.format(len_clean, len_noise))
        if len_noise > len_clean:
            #noise = noise[0:len(clean)]
            st = np.random.randint(0, len_noise - len_clean)
            noise = noise[st:st+len_clean]
        
        else:
        
            while len_noise<=len_clean:
                idx_n = idx_n + 1
                if idx_n >= np.size(noisefilenames)-1:
                    idx_n = np.random.randint(0, np.size(noisefilenames))
                newnoise, fs = audioread(noisefilenames[idx_n])
                while newnoise is None: # or len(noise)/fs < 3:
                    idx_s = np.random.randint(0, np.size(noisefilenames))
                    newnoise, fs = audioread(noisefilenames[idx_s])
                if fs != target_fs:
                    newnoise = librosa.resample(newnoise, fs, target_fs)
                noiseconcat = np.append(noise, np.zeros(int(target_fs*silence_length)))
                noise = np.append(noiseconcat, newnoise)
                len_noise = len(noise)
                noisefilename = noisefilenames[idx_n].split("/")[-1]
                noisefilename = noisefilename.split(".")[0]
                noise_files.append(noisefilename)

            len_noise = len(noise)
            st = np.random.randint(0, len_noise - len_clean)
            noise = noise[st:st+len_clean]
            #noise = noise[0:len(clean)]
        filecounter = filecounter + 1

        ##generate some extra SNR values
        if Random_SNR_flag:
            SNR_tmp = [] #SNR.copy()
            for i in range(0, 1):
                snr_rand = round(np.random.rand(1)[0] * (SNR[-1]-SNR[0]) + SNR[0],2)
                while snr_rand in SNR_tmp:
                    snr_rand = round(np.random.rand(1)[0] * (SNR[-1]-SNR[0]) + SNR[0],2)
                SNR_tmp.append(snr_rand)
        else:
            SNR_tmp = SNR
        print('SNR_tmp: {}'.format(SNR_tmp)) 

        noise_str = '_'.join(noise_files)
        clean_str = '_'.join(clean_files)

        for i in range(len(SNR_tmp)):
            clean_snr, noise_snr, noisy_snr = snr_mixer_weak_voice(clean=clean, noise=noise, target=target,snr=SNR_tmp[i], fs=target_fs)
            if Test_flag:
                noisyfilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'_speech_'+clean_str+'_noise_'+noise_str+'.wav'
                cleanfilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'_speech_'+clean_str+'_noise_'+noise_str+'.wav'
                noisefilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'_speech_'+clean_str+'_noise_'+noise_str+'.wav'
            else:
                noisyfilename = cleanfilename
                cleanfilename = cleanfilename
                noisefilename = cleanfilename

            
            f.write(noisyfilename+'\n')
            noisypath = os.path.join(noisyspeech_dir, noisyfilename)
            cleanpath = os.path.join(clean_proc_dir, cleanfilename)
            audiowrite(noisy_snr, target_fs, noisypath, norm=False)
            audiowrite(clean_snr, target_fs, cleanpath, norm=False)
            if Save_noise:
                noisepath = os.path.join(noise_proc_dir, noisefilename)
                audiowrite(noise_snr, target_fs, noisepath, norm=False)
            num_samples = num_samples + len(noisy_snr)
        count += 1
    f.close()
if __name__=="__main__":
    parser = argparse.ArgumentParser()
    
    # Configurations: read parameter.cfg
    parser.add_argument("--cfg", default = "config/para.cfg", help = "Read cfg for all the details")
    parser.add_argument("--cfg_str", type=str, default = "noisy_speech" )
    parser.add_argument('--output_path', type=str, default='./data', help='output will be save to this path')
    parser.add_argument('--output_reverb_dir', type=str, default='output_reverb_speech', help='output reverb speech will be save to this dir')
    parser.add_argument('--output_reverb_noisy_dir', type=str, default='output_reverb_noisy_speech', help='output reverb and noisy speech will be save to this dir')
    parser.add_argument('--noise_scp', type=str, default='./data/data_scp/noise.scp', help='scp file to storing noise wav list')
    parser.add_argument('--run_num', type=str, default='0', help='set to 0,1,2,3,... for different run')
    parser.add_argument('--sample_rate', type=int, default=48000, help='sampling rate for generated audio')
    args = parser.parse_args()

    
    cfgpath = os.path.join(os.path.dirname(__file__), args.cfg)
    assert os.path.exists(cfgpath), f"No configuration file as [{cfgpath}]"
    cfg = CP.ConfigParser()
    cfg._interpolation = CP.ExtendedInterpolation()
    cfg.read(cfgpath)
    
    main(cfg._sections[args.cfg_str], args)
    
