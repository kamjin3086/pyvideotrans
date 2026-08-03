import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Union, Dict, List

from openai import OpenAI, AuthenticationError, PermissionDeniedError, NotFoundError,APIConnectionError,APIError
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_not_exception_type, before_log, after_log

from videotrans.configure.config import tr,params, logger, settings
from videotrans.configure.excepts import NO_RETRY_EXCEPT, StopTask
from videotrans.tts._base import BaseTTS
from videotrans.util.help_misc import vail_file


@dataclass
class OPENAITTS(BaseTTS):
    def __post_init__(self):
        super().__post_init__()
        self.stop_next_all=False
        self.use_local_cli = os.environ.get('PYVIDEOTRANS_QWENTTS_CLI') == '1'
        self.api_url = params.get('openaitts_api','')
        if not self.use_local_cli and len(self.api_url)<10:
            raise StopTask(f'API URL is error: {self.api_url}')
        self.speed=self.get_speed()

    def _exec(self) -> None:
        if not self.use_local_cli:
            return super()._exec()

        pending = [item for item in self.queue_tts
                   if item.get('text', '').strip() and not vail_file(item['filename'])]
        if not pending:
            return

        # One qwen-tts process loads the model once, then emits one RIFF stream
        # per subtitle line.  Grouping preserves support for per-line roles.
        groups = {}
        for item in pending:
            groups.setdefault(item['role'], []).append(item)
        for role, items in groups.items():
            self._run_cli_batch(role, items)

    def _run_cli_batch(self, role: str, items: List[Dict]) -> None:
        binary = os.environ['PYVIDEOTRANS_QWENTTS_BIN']
        model = os.environ['PYVIDEOTRANS_QWENTTS_MODEL']
        codec = os.environ['PYVIDEOTRANS_QWENTTS_CODEC']
        for required in (binary, model, codec):
            if not Path(required).is_file():
                raise StopTask(f'Qwen TTS CLI file does not exist: {required}')

        command = [
            binary, '--model', model, '--codec', codec,
            '--speaker', role, '--lang', 'Chinese',
            '--seed', os.environ.get('PYVIDEOTRANS_QWENTTS_SEED', '42'),
            '--temp', os.environ.get('PYVIDEOTRANS_QWENTTS_TEMP', '0.62'),
            '--top-p', os.environ.get('PYVIDEOTRANS_QWENTTS_TOP_P', '0.9'),
        ]
        instruction = os.environ.get('PYVIDEOTRANS_QWENTTS_INSTRUCT', '').strip()
        if instruction:
            command.extend(['--instruct', instruction])
        command.extend(['--stream-by-line', '-o', '-'])

        input_text = '\n'.join(re.sub(r'[\r\n]+', ' ', item['text']).strip()
                               for item in items) + '\n'
        logger.debug(f'Qwen TTS CLI batch: role={role}, lines={len(items)}')
        result = subprocess.run(
            command,
            input=input_text.encode('utf-8'),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=1800,
        )
        if result.returncode != 0:
            detail = result.stderr.decode('utf-8', errors='replace')[-4000:]
            raise StopTask(f'Qwen TTS CLI failed ({result.returncode}): {detail}')

        offsets = [match.start() for match in re.finditer(b'RIFF', result.stdout)]
        if len(offsets) != len(items):
            detail = result.stderr.decode('utf-8', errors='replace')[-2000:]
            raise StopTask(
                f'Qwen TTS CLI returned {len(offsets)} WAV streams for '
                f'{len(items)} subtitle lines: {detail}'
            )

        offsets.append(len(result.stdout))
        for index, item in enumerate(items):
            wav_data = bytearray(result.stdout[offsets[index]:offsets[index + 1]])
            # stdout streaming uses sentinel RIFF/data lengths.  Replace them
            # with the actual per-line sizes before passing the file to FFmpeg.
            if len(wav_data) < 44 or wav_data[8:12] != b'WAVE':
                raise StopTask(f'Qwen TTS CLI returned an invalid WAV at line {index + 1}')
            wav_data[4:8] = (len(wav_data) - 8).to_bytes(4, 'little')
            data_pos = wav_data.find(b'data')
            if data_pos < 0 or data_pos + 8 > len(wav_data):
                raise StopTask(f'Qwen TTS CLI WAV has no data chunk at line {index + 1}')
            wav_data[data_pos + 4:data_pos + 8] = (len(wav_data) - data_pos - 8).to_bytes(4, 'little')

            raw_path = item['filename'] + '.cli.wav'
            Path(raw_path).write_bytes(wav_data)
            try:
                if not self.convert_to_wav(raw_path, item['filename']):
                    raise StopTask(f'Failed to convert Qwen TTS CLI WAV: {item["filename"]}')
            finally:
                Path(raw_path).unlink(missing_ok=True)
            self.signal(text=f'{tr("Dubbing")} [{index + 1}/{len(items)}]')


    @retry(retry=retry_if_not_exception_type(NO_RETRY_EXCEPT), stop=(stop_after_attempt(settings.get('retry_nums'))), wait=wait_fixed(2), before=before_log(logger, logging.INFO), after=after_log(logger, logging.INFO))
    def _run(self, data_item: Union[Dict, List, None], idx: int = -1) -> Union[str, None]:
        if vail_file(data_item['filename']):return
        try:
            client = OpenAI(api_key=params.get('openaitts_key', ''), base_url=self.api_url)
            speech_params = {
                "model": params.get('openaitts_model', ''),
                "voice": data_item['role'],
                "input": data_item['text'],
                "speed": self.speed,
                "response_format": "wav",
            }
            instructions = params.get('openaitts_instructions', '').strip()
            if instructions:
                speech_params["instructions"] = instructions
            with client.audio.speech.with_streaming_response.create(**speech_params) as response:
                with open(data_item['filename'] + ".wav", 'wb') as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)
            self.convert_to_wav(data_item['filename'] + ".wav", data_item['filename'])
        except APIConnectionError as e:
            raise StopTask(f'[OpenAITTS] {tr("Unable to connect to API",self.api_url)}\n{e}') from e
        except (NotFoundError,AuthenticationError, PermissionDeniedError) as e:
            raise StopTask(e.message)
        except APIError as e: 
            if re.search(r"insufficient.*?balance",e.message,flags=re.I):
                raise StopTask(tr('The server returned an error message: Insufficient balance',tr('OpenAI-TTS'),self.api_url))
            raise
