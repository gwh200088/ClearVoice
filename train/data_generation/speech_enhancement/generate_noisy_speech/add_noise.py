"""
This piece of program is modified based on MS-SNSD software
"""
import glob
import numpy as np
import soundfile as sf
import os
import argparse
import configparser as CP
from audiolib import audioread, audiowrite, snr_mixer, get_filenames, snr_mixer_weak_voice
import librosa

def main(cfg, args):
    snr_lower = float(cfg["snr_lower"])
    snr_upper = float(cfg["snr_upper"])
    total_snrlevels = float(cfg["total_snrlevels"])
    
    if cfg["speech_dir"]!='None':
        clean_dir = cfg["speech_dir"]
        if not os.path.exists(clean_dir):
            assert False, ("Clean speech data is required")
    
    if cfg["noise_dir"]!='None':
        noise_dir = cfg["noise_dir"]
        if not os.path.exists(noise_dir):
            assert False, ("Noise data is required")
    if cfg["clean_list"]!='None':
        clean_list = cfg["clean_list"]
    if cfg["noise_list"]!='None':
        noise_list = cfg["noise_list"]

    target_fs = int(cfg["sampling_rate"])
    audioformat = cfg["audioformat"]
    total_hours = float(cfg["total_hours"])
    min_audio_length = float(cfg["min_audio_length"])
    max_audio_length = float(cfg["max_audio_length"])
    silence_length = float(cfg["silence_length"])
    Test_flag = cfg["test"]=="True"
    print('Test_flag: {}'.format(Test_flag))
    Save_noise = cfg["save_noise"]=="True"
    print('Save_noise: {}'.format(Save_noise))

    Random_SNR_flag = cfg["random_snr"]=="True"
    print('Random_SNR_flag: {}'.format(Random_SNR_flag))
    suffix = cfg["suffix"]
    out_audio_root = cfg["out_audio_root"] + '/run' + args.run_num + '/' + suffix
    noisyspeech_dir = os.path.join(out_audio_root, 'noisy')
    if not os.path.exists(noisyspeech_dir):
        os.makedirs(noisyspeech_dir)
    clean_proc_dir = os.path.join(out_audio_root, 'target')
    if not os.path.exists(clean_proc_dir):
        os.makedirs(clean_proc_dir)
    noise_proc_dir = os.path.join(out_audio_root, 'noise')
    if not os.path.exists(noise_proc_dir) and Save_noise:
        os.makedirs(noise_proc_dir)
    wav_list = out_audio_root+'/wav.scp'
    f = open(wav_list, 'w')
    
    total_secs = total_hours*60*60
    total_samples = int(total_secs * target_fs)
    min_audio_length = int(min_audio_length*target_fs)
    max_audio_length = int(max_audio_length*target_fs)
    SNR = np.linspace(snr_lower, snr_upper, int(total_snrlevels))
    SNR = SNR.tolist()

    if cfg["speech_dir"]!='None':
        cleanfilenames = glob.glob(os.path.join(clean_dir, audioformat))
    else:
        cleanfilenames = get_filenames(clean_list)
    if cfg["noise_dir"]!='None':
        if cfg["noise_types_excluded"]=='None':
            noisefilenames = glob.glob(os.path.join(noise_dir, audioformat))
        else:
            filestoexclude = cfg["noise_types_excluded"].split(',')
            noisefilenames = glob.glob(os.path.join(noise_dir, audioformat))
            for i in range(len(filestoexclude)):
                noisefilenames = [fn for fn in noisefilenames if not os.path.basename(fn).startswith(filestoexclude[i])]
    else:
        noisefilenames = get_filenames(noise_list)
    
    filecounter = 0
    num_samples = 0
    print('total samples: {}'.format(total_samples))
    count = 0
    while num_samples < total_samples:
        clean_files = []
        noise_files = []
        print('current samples: {}'.format(num_samples))
        if Test_flag:
            idx_s = count
            while idx_s >= len(cleanfilenames):
                idx_s = idx_s - len(cleanfilenames)
        else:
            idx_s = np.random.randint(0, np.size(cleanfilenames))
        clean, fs = audioread(cleanfilenames[idx_s])
        while clean is None or len(clean)/fs < 3:
            idx_s = np.random.randint(0, np.size(cleanfilenames))
            clean, fs = audioread(cleanfilenames[idx_s])
        if fs != target_fs:
            clean = librosa.resample(clean, fs, target_fs)
        cleanfilename = cleanfilenames[idx_s].split("/")[-1]
        cleanfilename = cleanfilename.split(".")[0]
        clean_files.append(cleanfilename)
        len_clean = len(clean)
        if len_clean>min_audio_length:
            clean = clean
            if len_clean > max_audio_length:
                #clean = clean[0:max_audio_length]
                st = np.random.randint(0, len_clean-max_audio_length)
                clean = clean[st:st+max_audio_length]
        else:
            
            while len_clean<=min_audio_length:
                idx_s = idx_s + 1
                if idx_s >= np.size(cleanfilenames)-1:
                    idx_s = np.random.randint(0, np.size(cleanfilenames)) 
                newclean, fs = audioread(cleanfilenames[idx_s])
                while newclean is None or len(newclean)/fs < 3:
                    idx_s = np.random.randint(0, np.size(cleanfilenames))
                    newclean, fs = audioread(cleanfilenames[idx_s])
                if fs != target_fs:
                    newclean = librosa.resample(newclean, fs, target_fs)
                cleanconcat = np.append(clean, np.zeros(int(target_fs*silence_length)))
                clean = np.append(cleanconcat, newclean)
                len_clean = len(clean)
                cleanfilename = cleanfilenames[idx_s].split("/")[-1]
                cleanfilename = cleanfilename.split(".")[0]
                clean_files.append(cleanfilename)
            len_clean = len(clean)
            if len_clean > max_audio_length:
                #clean = clean[0:max_audio_length]
                st = np.random.randint(0, len_clean-max_audio_length)
                clean = clean[st:st+max_audio_length]

        if Test_flag:
            idx_n = count
            while idx_n >= len(noisefilenames):
                idx_n = idx_n - len(noisefilenames)
        else:
            idx_n = np.random.randint(0, np.size(noisefilenames))
        noise, fs = audioread(noisefilenames[idx_n])
        while noise is None: # or len(noise)/fs < 3:
            idx_s = np.random.randint(0, np.size(noisefilenames))
            noise, fs = audioread(noisefilenames[idx_s])
        if fs != target_fs:
            noise = librosa.resample(noise, fs, target_fs)
        noisefilename = noisefilenames[idx_n].split("/")[-1]
        noisefilename = noisefilename.split(".")[0]
        noise_files.append(noisefilename)        
        len_noise = len(noise)
        len_clean = len(clean)
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
            SNR_tmp = []
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
            clean_snr, noise_snr, noisy_snr = snr_mixer_weak_voice(clean=clean, noise=noise, snr=SNR_tmp[i], fs=target_fs)
            if Test_flag:
                noisyfilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'_speech_'+clean_str+'_noise_'+noise_str+'.wav'
                cleanfilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'_speech_'+clean_str+'_noise_'+noise_str+'.wav'
                noisefilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'_speech_'+clean_str+'_noise_'+noise_str+'.wav'
            else:
                noisyfilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'.wav'
                cleanfilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'.wav'
                noisefilename = suffix +str(filecounter)+'_SNRdb_'+str(SNR_tmp[i])+'.wav'

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
    parser.add_argument("--run_num", type=str, default = "0", help="set 0,1,2,3,... for each new run" )
    args = parser.parse_args()

    
    cfgpath = os.path.join(os.path.dirname(__file__), args.cfg)
    assert os.path.exists(cfgpath), f"No configuration file as [{cfgpath}]"
    cfg = CP.ConfigParser()
    cfg._interpolation = CP.ExtendedInterpolation()
    cfg.read(cfgpath)
    
    main(cfg._sections[args.cfg_str], args)
    
