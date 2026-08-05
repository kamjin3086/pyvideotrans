import copy
import json
import os
import re
import shutil
import time
from pathlib import Path

from videotrans.configure._paths import DUBBING_CACHE
from videotrans.configure.config import tr, app_cfg, settings, logger
from videotrans.configure.excepts import DubbingSrtError
from videotrans.tts import run as run_tts, SUPPORT_CLONE
from videotrans.util.help_misc import get_md5, vail_file
from videotrans.util.help_srt import get_subtitle_from_srt, delete_punc


class DubbingMixin:

    def dubbing(self) -> None:
        _st=time.time()
        if self._exit() or self.cfg.app_mode == 'tiqu':
            return
        if self.should_dubbing:
            self.signal(text=tr('kaishipeiyin'))
        self.precent += 3
        self._tts()
        
        if  Path(self.cfg.source_sub).exists():
            logger.debug('配音结束后，移除原始字幕中所有标点')
            subs = get_subtitle_from_srt(self.cfg.source_sub)
            for it in subs:
                if self.cfg.fix_punc==2:
                    it['text']=delete_punc(it['text'])
                it['text']=it['text'].strip('...')
            self._save_srt_target(subs, self.cfg.source_sub)
        if self.should_dubbing:
            self.signal(text=tr('The dubbing is finished'))
            logger.debug(f'[语音合成阶段结束耗时]:{time.time()-_st}s')

    def _tts(self) -> None:
        if not self.should_dubbing:
            self.signal(text='Skip tts')
            return
        queue_tts = []
        subs = get_subtitle_from_srt(self.cfg.target_sub)
        source_subs = get_subtitle_from_srt(self.cfg.source_sub)
        if len(subs) < 1:
            raise DubbingSrtError(f"SRT file error:{self.cfg.target_sub}")
        try:
            rate = int(str(self.cfg.voice_rate).replace('%', ''))
        except (ValueError,TypeError):
            rate = 0

        rate = f"+{rate}%" if rate >= 0 else f"{rate}%"

        line_roles = app_cfg.line_roles
        voice_role = self.cfg.voice_role
        female_voice = os.environ.get('PYVIDEOTRANS_QWENTTS_FEMALE_VOICE', 'female-01')
        if os.environ.get('PYVIDEOTRANS_AUTO_VOICE_STYLE', '0') == '1':
            try:
                from videotrans.process.voice_style import choose_video_voice_plan
                voice_plan = choose_video_voice_plan(
                    source_subs,
                    subs,
                    default_male=voice_role,
                    male_requested=os.environ.get('PYVIDEOTRANS_VOICE_PROFILE_REQUESTED', 'auto'),
                    female_requested=os.environ.get('PYVIDEOTRANS_FEMALE_VOICE_PROFILE', 'auto'),
                    api_base=os.environ.get('PYVIDEOTRANS_LLM_API', 'http://127.0.0.1:8101/v1'),
                    model=os.environ.get('PYVIDEOTRANS_LLM_MODEL', 'Qwen3.6-35B-A3B-instruct'),
                    profile_path=os.environ.get('PYVIDEOTRANS_VOICE_STYLE_PROFILE'),
                    metadata_context=os.environ.get('PYVIDEOTRANS_VIDEO_STYLE_CONTEXT', ''),
                )
                voice_role = voice_plan['male_voice']
                female_voice = voice_plan['female_voice']
                Path(self.cfg.target_dir, 'voice-style-plan.json').write_text(
                    json.dumps(voice_plan, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8',
                )
                logger.debug(
                    f'视频配音风格完成: style={voice_plan["style"]}, '
                    f'male={voice_role}, female={female_voice}'
                )
            except Exception as exc:
                logger.exception(f'视频配音风格分类失败，继续使用默认音色: {exc}', exc_info=True)
        if (
            os.environ.get('PYVIDEOTRANS_AUTO_VOICE_GENDER', '0') == '1'
            and source_subs
            and self.cfg.vocal
            and Path(self.cfg.vocal).is_file()
        ):
            try:
                from videotrans.process.voice_pitch import route_subtitle_voices
                inferred_roles, report = route_subtitle_voices(
                    self.cfg.vocal,
                    source_subs,
                    default_voice=voice_role,
                    female_voice=female_voice,
                    qwen_codec_bin=os.environ.get('PYVIDEOTRANS_QWEN_CODEC_BIN'),
                    codec_model=os.environ.get('PYVIDEOTRANS_QWENTTS_CODEC'),
                    base_model=os.environ.get('PYVIDEOTRANS_QWENTTS_BASE_MODEL'),
                    male_prototype_path=os.environ.get('PYVIDEOTRANS_VOICE_MALE_PROTOTYPE'),
                    female_prototype_path=os.environ.get('PYVIDEOTRANS_VOICE_FEMALE_PROTOTYPE'),
                )
                # Explicit per-line assignments remain authoritative.
                line_roles = inferred_roles | (line_roles or {})
                Path(self.cfg.target_dir, 'voice-routing.json').write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8',
                )
                logger.debug(f'自动声线路由完成: {report["counts"]}')
            except Exception as exc:
                logger.exception(f'自动声线路由失败，继续使用默认音色: {exc}', exc_info=True)
        logger.debug(f'{line_roles=}')
        for i, it in enumerate(subs):
            if it['end_time'] < it['start_time'] or not it['text'].strip():
                continue
            voice = line_roles.get(f'{it["line"]}', voice_role) if line_roles else voice_role

            _key = get_md5(f"{self.cfg.target_language_code}-{it['text']}-{voice}-{rate}-{self.cfg.volume}-{self.cfg.pitch}-{self.cfg.tts_type}")

            tmp_dict = {
                "text": it['text'],
                "line": it['line'],
                "start_time": it['start_time'],
                "end_time": it['end_time'],
                "startraw": it['startraw'],
                "endraw": it['endraw'],
                "ref_text": source_subs[i]['text'] if source_subs and i < len(source_subs) else '',
                "start_time_source": source_subs[i]['start_time'] if source_subs and i < len(source_subs) else it[
                    'start_time'],
                "end_time_source": source_subs[i]['end_time'] if source_subs and i < len(source_subs) else it[
                    'end_time'],
                "role": voice,
                "rate": rate,
                "volume": self.cfg.volume,
                "pitch": self.cfg.pitch,
                "tts_type": self.cfg.tts_type,
                "filename": f"{self.cfg.cache_folder}/{i}-{_key}.wav"
            }
            _dubbing_cache=f'{DUBBING_CACHE}/{_key}.wav'
            if vail_file(_dubbing_cache):
                # 直接使用缓存
                shutil.copy2(_dubbing_cache,tmp_dict['filename'])
            if str(voice).strip().lower() == 'clone' and self.cfg.tts_type in SUPPORT_CLONE:
                tmp_dict['ref_wav'] = f"{self.cfg.cache_folder}/clone-{i}.wav"
                tmp_dict['ref_language'] = self.cfg.detect_language[:2]
            queue_tts.append(tmp_dict)

        self.queue_tts = copy.deepcopy(queue_tts)

        if not self.queue_tts or len(self.queue_tts) < 1:
            raise RuntimeError(f'字幕长度为0，无法继续配音')

        if len([it.get("ref_wav") for it in self.queue_tts if it.get("ref_wav")]) > 0:
            self._create_ref_from_vocal()

        run_tts(
            queue_tts=self.queue_tts,
            language=self.cfg.target_language_code,
            uuid=self.uuid,
            tts_type=self.cfg.tts_type,
            is_cuda=self.cfg.is_cuda
        )
        outname=None
        if settings.get('save_segment_audio', False):
            outname = self.cfg.target_dir + f'/segment_audio_{self.cfg.noextname}'
            Path(outname).mkdir(parents=True, exist_ok=True)
        for it in self.queue_tts:
            it['text']=it['text'].strip('...')
            if self.cfg.fix_punc==2:
                it['text']=delete_punc(it['text'])
            if Path(it['filename']).exists():
                # 保存缓存
                shutil.copy2(it['filename'],f'{DUBBING_CACHE}/'+Path(it['filename']).name.split('-')[-1])
                if outname:
                    text = re.sub(r'["\'*?\\/|:<>\r\n\t]+', '', it['text'], flags=re.I | re.S)
                    name = f'{outname}/{it["line"]}-{text[:60]}.wav'
                    shutil.copy2(it['filename'], name)
        
