import ctypes
import logging
import os
import re
import sys
import tempfile
import threading
import unicodedata
from tkinter import *
from tkinter import filedialog, messagebox
from tkinter.ttk import *
from typing import Any, Callable, Union

if sys.platform == 'win32':
    import winreg

import requests
import yt_dlp
from sanitize_filename import sanitize
from caption_translation import translate_sidecar, find_sidecar
from asr_captions import generate_captions_asr
from dubbing_pipeline import run_dubbing_pipeline, resolve_ffmpeg, get_available_engines, test_voice_sample

frozen = getattr(sys, 'frozen', False)  # frozen -> running in exe
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.addLevelName(15, 'STATUS')  # between debug 10 and info 20
logger.setLevel(15)
download_queue = []
ongoing_task: bool = False
_temp_dir = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'Temp', 'yt-dlp-gui')
os.makedirs(_temp_dir, exist_ok=True)
os.environ['TMP'] = os.environ['TEMP'] = _temp_dir
tempfile.tempdir = _temp_dir
try:
    os.chdir(os.path.expanduser('~'))
except OSError:
    pass
logger.info(f'YT-DLP GUI (yt-dlp {yt_dlp.version.__version__}) (Python {sys.version})')


def get_res_path(relative_path: str) -> str:
    """ Get absolute path to resource, works for dev and for PyInstaller
     Relative path will always get extracted into root!"""
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(__file__))
    if os.path.exists(os.path.join(base_path, relative_path)):
        return os.path.join(base_path, relative_path)
    else:
        raise FileNotFoundError(f'{os.path.join(base_path, relative_path)} is not found!')


ydl_base_opts: dict[str, Any] = {'outtmpl': 'TITLE-%(id)s.%(ext)s',
                                 'restrictfilenames': True,
                                 'nocheckcertificate': True,
                                 'ignoreerrors': False,
                                 'logtostderr': False,
                                 'geo-bypass': True,
                                 'quiet': True,
                                 'no_warnings': True,
                                 'default_search': 'auto',
                                 'source_address': '0.0.0.0',
                                 'windowsfilenames': True,
                                 'overwrites': True,
                                 'cachedir': False,
                                 'age_limit': 100,
                                 'noplaylist': True,
                                 'live_from_start': True,
                                 'no-video-multistreams': True,
                                 'no-audio-multistreams': True,
                                 'check_formats': 'selected',
                                 'fixup': 'detect_or_warn',
                                 'extractor_args': {'youtube': {'skip': ['dash', 'hls']}, },
                                 'ffmpeg_location': 'ffmpeg',
                                 }
percent_str_regex = re.compile(r'\d{1,3}\.\d{1,2}%')


def _safe_file_stem(text: str, video_id: str = '') -> str:
    """ASCII-safe filesystem stem; avoids Chinese/emoji path issues."""
    text = unicodedata.normalize('NFKD', text or '')
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^A-Za-z0-9._-]+', '_', text).strip('._-')
    if not text:
        text = 'video'
    return text


def handle_private_video(e: object, url: str, ydl_opts: dict, func: Callable[[str, dict, bool], Union[dict, bool]]) -> Union[dict, bool]:
    """Returns info if func is successful after login and False if not"""
    result = False

    def handle_choose():
        nonlocal result
        if v.get() == 0: return
        browser = [browsers[v.get() - 1]]  # yt_dlp expects a list
        ydl_opts['cookiesfrombrowser'] = browser
        popup.title('Logging in...')
        choose_button_var.set('Logging in...')
        choose_button.configure(state=DISABLED)
        popup.update()
        info = func(url, ydl_opts, True)  # can be download() or extract_info()
        if info:
            ydl_base_opts['cookiesfrombrowser'] = browser
            result = info
            messagebox.showinfo('Login', 'Login successful! Specified browser will be used until you close this app. Browser will be asked again if you attempt to download another private video that the existing accounts in this browser cannot access.')
        else:
            messagebox.showerror('Login', 'Login failed. The browser you chose does not have an account with access to this video.')
        popup.destroy()

    if str(e).endswith('Private video. Sign in if you\'ve been granted access to this video'):
        browsers = ['brave', 'chrome', 'chromium', 'edge', 'firefox', 'opera', 'safari', 'vivaldi']
        popup = Toplevel(takefocus=True)
        popup.title('Choose browser to fetch account cookies from')
        Label(popup, text='This video is private. Please choose the browser you use that is logged into your video platform.\nIf you are on Windows and use Chrome or Edge, please close your browser before choosing.').pack()
        v = IntVar(value=0)
        choose_button_var = StringVar(value='Choose')
        for i, option in enumerate(browsers):
            Radiobutton(popup, text=option, variable=v, value=i + 1).pack(anchor="w")
        choose_button = Button(popup, command=handle_choose, textvariable=choose_button_var)
        choose_button.pack()
        root.wait_window(popup)
    return result


def download(urls: Union[list, str], ydl_opts=None, ignore_error: bool = False) -> bool:
    """Return whether download is successful"""
    if ydl_opts is None: ydl_opts = ydl_base_opts.copy()
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download(urls if isinstance(urls, list) else [urls])
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as e:
        if ignore_error: return False
        return handle_private_video(e, urls, ydl_opts, download)
    except Exception as e:
        messagebox.showerror('Error', f'Error while downloading: {e}')
        return False
    else:
        return True


def extract_info(url: str, ydl_opts: dict, ignore_error: bool = False) -> dict:
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return ydl.sanitize_info(info)
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as e:
        if ignore_error: return {}
        result = handle_private_video(e, url, ydl_opts, extract_info)
        if not result:
            return {}
        else:  # success
            return result
    except Exception as e:
        messagebox.showerror('Error', f'Error while extracting info: {e}')
        return {}


class ScrolledWindow(Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.parent = parent

        # creating scrollbars
        self.xscrlbr = Scrollbar(self.parent, orient=HORIZONTAL)
        self.xscrlbr.pack(fill=X, side=BOTTOM)
        self.yscrlbr = Scrollbar(self.parent)
        self.yscrlbr.pack(fill=Y, side=RIGHT)

        # creating canvas
        self.canv = Canvas(self.parent)
        # noinspection PyArgumentList
        self.canv.config(relief='flat', width=10, heigh=10, bd=2)
        self.canv.pack(side=TOP, fill=BOTH, expand=True)

        # accociating scrollbar comands to canvas scroling
        self.xscrlbr.config(command=self.canv.xview)
        self.yscrlbr.config(command=self.canv.yview)

        # creating a frame to inserto to canvas
        self.scrollwindow = Frame(self.parent)
        self.canv.create_window(0, 0, window=self.scrollwindow, anchor=NW)
        self.canv.config(xscrollcommand=self.xscrlbr.set, yscrollcommand=self.yscrlbr.set, scrollregion=self.canv.bbox('all'))
        self.yscrlbr.lift(self.scrollwindow)
        self.xscrlbr.lift(self.scrollwindow)
        self.scrollwindow.bind('<Configure>', self._configure_window)
        self.scrollwindow.bind('<Enter>', self._bound_to_mousewheel)
        self.scrollwindow.bind('<Leave>', self._unbound_to_mousewheel)

    def _bound_to_mousewheel(self, event):
        self.canv.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbound_to_mousewheel(self, event):
        self.canv.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self.canv.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _configure_window(self, event):
        # update the scrollbars to match the size of the inner frame
        size = (self.scrollwindow.winfo_reqwidth(), self.scrollwindow.winfo_reqheight())
        # noinspection PyTypeChecker
        self.canv.config(scrollregion='0 0 %s %s' % size)
        if self.scrollwindow.winfo_reqwidth() != self.canv.winfo_width():
            # update the canvas's width to fit the inner frame
            self.canv.config(width=self.scrollwindow.winfo_reqwidth())
        if self.scrollwindow.winfo_reqheight() != self.canv.winfo_height():
            # update the canvas's width to fit the inner frame
            self.canv.config(height=self.scrollwindow.winfo_reqheight())


try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except:
    pass


class DownloadTask(Frame):
    def __init__(self, url: str, path: str, ydl_opts: dict, parent: Widget):
        super().__init__(parent, borderwidth=2, relief='groove')
        self.url = url
        self.parent = parent
        self.ydl_opts = ydl_opts
        self.save_path = path
        self.video_id = None
        self.translate_captions = bool(self.ydl_opts.pop('translate_captions_tr', False))
        self.translation_backend = self.ydl_opts.pop('translation_backend', 'groq')
        self.asr_captions = bool(self.ydl_opts.pop('asr_captions', False))
        self.dub_turkish = bool(self.ydl_opts.pop('dub_turkish', False))
        self.dub_engine = self.ydl_opts.pop('dub_engine', get_available_engines()[0] if get_available_engines() else 'Edge TTS [online] (Ahmet - Erkek)')
        self._translation_started = False
        self.thread = None
        self.ydl_opts['progress_hooks'] = [self.progress_hook]
        self.ydl_opts['postprocessor_hooks'] = [self.postprocessor_hook]
        self.status = StringVar(value='Queued')
        self.progress = IntVar(value=0)

        self.extract_info_succeed = False
        info = extract_info(url, ydl_opts)
        if not info: return
        parsed_info = parse_info(info)
        self.extract_info_succeed = True
        self.video_id = info.get('id')
        self.title = sanitize(parsed_info['title'])
        self.file_title = _safe_file_stem(self.title, self.video_id or '')
        self.duration = parsed_info['duration']
        self.size = parsed_info['size']
        self.formats = parsed_info['formats']
        if isinstance(self.ydl_opts['outtmpl'], dict):
            self.ydl_opts['outtmpl']['default'] = self.ydl_opts['outtmpl']['default'].replace('TITLE', self.file_title)
        else:
            self.ydl_opts['outtmpl'] = self.ydl_opts['outtmpl'].replace('TITLE', self.file_title)

        Label(self, text=self.title).pack(side=TOP)
        details_frame = Frame(self)
        details_frame.pack(side=TOP)
        Label(details_frame, text=f'Duration: {self.duration}').pack(side=LEFT)
        Label(details_frame, text=f'Size: {round(self.size / (1024 * 1024), 2)}MB' if self.size else 'unknown size').pack(side=LEFT)
        format_str = ''
        if self.formats['video']: format_str += f'Video: {self.formats["video"]["resolution"]}@{self.formats["video"]["fps"]}fps {self.formats["video"]["codec"]} {"HDR" if self.formats["video"]["hdr"] else ""}'
        if self.formats['audio']: format_str += f'Audio: {self.formats["audio"]["sample_rate"]} {self.formats["audio"]["bitrate"]} {self.formats["audio"]["codec"]}'
        Label(details_frame, text=format_str).pack(side=LEFT)
        Label(self, text=f'Save to: {path}').pack(side=TOP)
        self.progress_bar = Progressbar(self, orient=HORIZONTAL, length=100, mode='determinate', variable=self.progress, value=0)
        self.progress_bar.pack(side=TOP, fill=X, expand=True, padx=10)
        Label(self, textvariable=self.status, anchor=CENTER).pack(side=TOP, fill=X, expand=True, padx=10)
        self.pack(side=TOP, fill=X, pady=(0, 5), ipadx=5)

    def start_task(self):
        global ongoing_task
        status('Downloading')
        self.thread = threading.Thread(target=self._run_task, daemon=True)
        self.thread.start()
        ongoing_task = True

    def _run_task(self):
        global ongoing_task
        success = False
        try:
            success = download(self.url, self.ydl_opts)
            if not success:
                return
            if self.asr_captions or self.translate_captions or self.dub_turkish:
                # ASR fallback: generate captions when no subtitle sidecar exists
                if self.asr_captions:
                    sidecar = find_sidecar(self.save_path, self.title, self.video_id or '', '')
                    if not sidecar:
                        src_video = self._find_source_video()
                        if src_video:
                            root.after(0, lambda: self.status.set('Generating captions from audio (ASR)...'))
                            res = self._generate_asr(src_video)
                            if res != 'done':
                                root.after(0, lambda: self.status.set('Finished (ASR incomplete)'))
                                return
                if self.translate_captions:
                    root.after(0, lambda: self.status.set('Translating captions...'))
                    self._translate_sidecar(self.translation_backend)
                if self.dub_turkish:
                    root.after(0, lambda: self.status.set('Dubbing to Turkish...'))
                    self._run_dubbing()
        finally:
            ongoing_task = False
            root.after(0, lambda: status('Ready', log=False))

    def _find_source_video(self) -> str:
        import glob as _glob
        stem = f'{self.file_title}-{self.video_id}'
        video_exts = ['.mp4', '.mkv', '.webm', '.mov', '.avi']
        for ext in video_exts:
            candidates = _glob.glob(os.path.join(self.save_path, stem + ext))
            if candidates:
                return candidates[0]
        return ''

    def _generate_asr(self, src_video: str) -> str:
        def set_status(text):
            root.after(0, lambda: self.status.set(text))
        asr_output = os.path.join(self.save_path, f'{self.file_title}-{self.video_id}.asr.srt')
        ffmpeg_exe = resolve_ffmpeg(os.path.dirname(os.path.abspath(__file__)))
        return generate_captions_asr(src_video, asr_output, ffmpeg_exe, set_status)

    def progress_hook(self, d: dict):
        global ongoing_task
        if d['status'] == 'downloading' and '_default_template' in d:
            percent_str = percent_str_regex.search(d['_percent_str'])
            if percent_str:
                percent_str = percent_str.group()
                self.status.set(f'Downloading: {d["_default_template"]}')
                self.progress.set(int(float(percent_str.rstrip('%'))))
        elif d['status'] == 'finished':  # may have postprocessing later but can start downloading next task already since postprocessing no need internet and is usually fast
            if self in download_queue: download_queue.remove(self)
            self.status.set('Finished')
            # ongoing_task is released by _run_task after download() returns (and translation, if any)
        elif d['status'] == 'error':
            if self in download_queue: download_queue.remove(self)
            ongoing_task = False
            self.status.set('Download error')
            status('Ready', log=False)

    def postprocessor_hook(self, d: dict):
        global ongoing_task
        if d['status'] == 'started' or d['status'] == 'processing':
            self.status.set('Post-processing')
        else:
            if self in download_queue: download_queue.remove(self)
            self.status.set('Finished')
            status('Ready', log=False)
            # do not set ongoing_ask = False here as the next download task may have already began

    def _translate_sidecar(self, backend='groq'):
        def set_status(text):
            root.after(0, lambda: self.status.set(text))

        try:
            langs = self.ydl_opts.get('subtitleslangs') or []
            lang = langs[0] if langs else ''
            sidecar = find_sidecar(self.save_path, self.file_title, self.video_id or '', lang)
            if not sidecar:
                root.after(0, lambda: status('Caption sidecar not found; translation skipped.'))
                set_status('Finished')
                return
            output_path = os.path.join(self.save_path, f'{self.file_title}-{self.video_id}.tr.srt')
            checkpoint_path = output_path + '.checkpoint.json'
            set_status('Translating captions...')
            result = translate_sidecar(sidecar, output_path, checkpoint_path, set_status, backend=backend)
            if result == 'done':
                set_status('Finished (translated)')
            elif result == 'missing_key':
                set_status('Finished (captions untranslated: no API key)')
            else:
                set_status('Finished (translation incomplete)')
        except Exception as e:
            error_text = str(e)
            root.after(0, lambda: status(f'Translation error: {error_text}'))
            set_status('Finished (translation error)')

    def _run_dubbing(self):
        """Translate caption -> TTS dub -> FFmpeg mux (online: Edge TTS)."""
        def set_status(text):
            root.after(0, lambda: self.status.set(text))

        try:
            tr_srt = os.path.join(self.save_path, f'{self.title}-{self.video_id}.tr.srt')
            if not os.path.exists(tr_srt):
                set_status('Dublaj: Turkce SRT bulunamadi; once ceviri tamamlanmis olmali.')
                return
            # Kaynak videoyu bul
            import glob as _glob
            stem = f'{self.file_title}-{self.video_id}'
            video_exts = ['.mp4', '.mkv', '.webm', '.mov', '.avi']
            src_video = None
            for ext in video_exts:
                candidates = _glob.glob(os.path.join(self.save_path, stem + ext))
                if candidates:
                    src_video = candidates[0]
                    break
            if not src_video:
                set_status('Dublaj: Kaynak video dosyasi bulunamadi.')
                return
            ffmpeg_exe = resolve_ffmpeg(os.path.dirname(os.path.abspath(__file__)))
            run_dubbing_pipeline(
                source_video=src_video,
                tr_srt_path=tr_srt,
                save_dir=self.save_path,
                title=self.title,
                video_id=self.video_id or '',
                ffmpeg_exe=ffmpeg_exe,
                engine_label=self.dub_engine,
                status_cb=set_status,
            )
        except Exception as e:
            error_text = str(e)
            root.after(0, lambda: status(f'Dublaj hatasi: {error_text[:120]}'))
            set_status('Finished (dubbing error)')


root = Tk()
root.iconphoto(True, PhotoImage(file=get_res_path('icon.png')))
root.title(f'YT-DLP GUI - Initializing')
status_var = StringVar(value='Initializing')
cookies_file_var = StringVar(value='')
status_bar = LabelFrame(root, padding=(2, 2))  # status bar must be at front else scrollable window will block it
status_bar.pack(expand=True, fill=X, side=BOTTOM, pady=(0, 0))

status_bar.rowconfigure(0, weight=1)
status_bar.columnconfigure(1, weight=1)
status_text = Label(status_bar, textvariable=status_var, anchor=E)
status_text.grid(row=0, column=2, sticky=E)

cookies_top_frame = Frame(root)
cookies_top_frame.pack(side=TOP, fill=X, padx=5, pady=5)
Label(cookies_top_frame, text='Cookies file (optional):').pack(side=LEFT)
Entry(cookies_top_frame, textvariable=cookies_file_var).pack(side=LEFT, fill=X, expand=True)


def status(text: Any, log: bool = True):
    global status_text
    text = text.strip()
    status_var.set(text)
    text = text.replace('\n', ' ')
    if log:
        if text.lower() == 'ready':
            logger.log(15, text + '\n')  # custom STATUS logging level 15
        else:
            logger.log(15, text)
    root.title(f'YT-DLP GUI - {text}')


def parse_info(info: dict, best_format_only: bool = True) -> dict:
    title = info['title']
    duration = info['duration_string']
    size = info.get('filesize', info.get('filesize_approx', 0))
    subtitles = info.get('subtitles', {})
    automatic_captions = info.get('automatic_captions', {})
    if best_format_only:  # only 1 for video and/or 1 for audio
        if 'requested_formats' in info:  # best video AND best audio
            temp = [parse_format(f) for f in info['requested_formats']]  # best video/best audio only
            formats = {}
            for f in temp:
                if f['video']: formats['video'] = f['video']
                if f['audio']: formats['audio'] = f['audio']
        else:  # best video OR audio only
            format_id = info['format_id']
            for f in info['formats']:
                if f['format_id'] == format_id:
                    formats = parse_format(f)
                    break
    else:
        formats = [parse_format(f) for f in info['formats'] if f.get('format_note', '') != 'storyboard' and (f.get('vcodec', 'none') != 'none' or f.get('acodec', 'none') != 'none') and f.get('url', '')]
    # noinspection PyUnboundLocalVariable
    return {'title': title, 'duration': duration, 'size': size, 'subtitles': subtitles, 'automatic_captions': automatic_captions, 'formats': formats}


def parse_format(format: dict) -> dict:
    parsed = {'video': {}, 'audio': {}}
    contains_video = format.get('vcodec', 'none') != 'none'
    contains_audio = format.get('acodec', 'none') != 'none'
    parsed['format_id'] = format['format_id']
    parsed['ext'] = format['ext']
    parsed['size'] = format.get('filesize', format.get('filesize_approx', 0))

    def parse_codec(codec) -> str:
        if not codec or codec == 'none':
            return 'none'
        mapping = {'mp4v': 'H263', 'av01': 'AV1', 'avc1': 'H264/AVC', 'hev1': 'H265/HEVC', 'vp9': 'VP9', 'vp09': 'VP9', 'vp8': 'VP8', 'mp4a': 'AAC', 'opus': 'Opus'}
        key = str(codec).split('.')[0].lower()
        return mapping.get(key, key)

    if contains_video: parsed['video'] = {'resolution': format.get('resolution', 'unknown'), 'fps': format.get('fps', 0),
                                          'codec': parse_codec(format.get('vcodec', 'none')),
                                          'hdr': format.get('dynamic_range', 'SDR') != 'SDR'}
    if contains_audio: parsed['audio'] = {'sample_rate': f'{round(format.get("asr", 0) / 1000, 2)}khz' if format.get('asr') else 'unknown sample rate',
                                          'bitrate': f'{round(format.get("abr", 0))}kbps' if format.get('abr', None) and float(format.get('abr', 0)) else 'unknown bitrate',
                                          'codec': parse_codec(format.get('acodec', 'none'))}
    return parsed


def check_url(url: str) -> bool:
    try:
        requests.get(url)
    except:
        return False
    return True


def handle_download_video_best(url: str, path: str):
    if not url:
        messagebox.showerror('Error', 'URL is empty!')
        return
    if not check_url(url):
        messagebox.showerror('Error', 'URL is invalid!')
        return
    ydl_opts = ydl_base_opts.copy()
    ydl_opts['outtmpl'] = os.path.join(path, ydl_opts['outtmpl'] if isinstance(ydl_opts['outtmpl'], str) else ydl_opts['outtmpl']['default'])
    download_queue.append(DownloadTask(url, path, ydl_opts, queue_frame))


def handle_download_audio_best(url: str, path: str):
    if not url:
        messagebox.showerror('Error', 'URL is empty!')
        return
    if not check_url(url):
        messagebox.showerror('Error', 'URL is invalid!')
        return
    ydl_opts = ydl_base_opts.copy()
    ydl_opts.update({'format': 'bestaudio'})
    ydl_opts['outtmpl'] = os.path.join(path, ydl_opts['outtmpl'] if isinstance(ydl_opts['outtmpl'], str) else ydl_opts['outtmpl']['default'])
    ydl_opts['outtmpl'] = ydl_opts['outtmpl'].replace('.%(ext)s', '_audio.%(ext)s')
    download_queue.append(DownloadTask(url, path, ydl_opts, queue_frame))


def handle_download_info(url: str, path: str, ydl_opts: dict = None):
    if not url:
        messagebox.showerror('Error', 'URL is empty!')
        return
    if not check_url(url):
        messagebox.showerror('Error', 'URL is invalid!')
        return
    if not ydl_opts: ydl_opts = ydl_base_opts.copy()
    if cookies_file_var.get().strip():
        ydl_opts['cookiefile'] = cookies_file_var.get().strip()
    status('Extracting info')
    info = extract_info(url, ydl_opts)
    if not info:
        status('Ready')
        return
    parsed_info = parse_info(info, best_format_only=False)
    details_window = Toplevel(takefocus=True)  # no need mainloop here as below we use the general global mainloop function
    details_window.title('Extracted Info')
    Label(details_window, text=f'{parsed_info["title"]} ({parsed_info["duration"]})', anchor=CENTER).pack(side=TOP, fill=X, expand=True, padx=10)

    scroll_container_frame = Frame(details_window)
    scroll_container_frame.pack(expand=True, fill=BOTH, side=TOP)
    scrollableFrame = ScrolledWindow(scroll_container_frame)
    formats_frame = LabelFrame(scrollableFrame.scrollwindow, text='Download Options')
    formats_frame.pack(side=TOP, fill=BOTH, expand=True, padx=(10, 10), pady=(10, 0))
    formats = parsed_info['formats']

    # noinspection PyArgumentList
    def on_select_single_format(*args):
        if video_only_formats[video_selected_format.get()] is None and audio_only_formats[audio_selected_format.get()] is None:  # if both selected nothing, unlock radio buttons
            for radio_button in radiobutton_frame.winfo_children():
                radio_button.configure(state=NORMAL)
            selected_format.set('')
        else:
            for radio_button in radiobutton_frame.winfo_children():
                radio_button.configure(state=DISABLED)
            selected = []
            if video_only_formats[video_selected_format.get()]: selected.append(video_only_formats[video_selected_format.get()])
            if audio_only_formats[audio_selected_format.get()]: selected.append(audio_only_formats[audio_selected_format.get()])
            selected_format.set('+'.join(selected))  # allow 1 only
        try:
            if video_only_formats[video_selected_format.get()] is None and audio_only_formats[audio_selected_format.get()] is not None:
                audio_convert_selector.configure(state=NORMAL)
            else:
                audio_convert_selector.configure(state=DISABLED)
                audio_convert_quality_selector.configure(state=DISABLED)
        except NameError:  # if audio convert selector is not defined yet
            pass

    def on_select_format(*args):
        download_button.configure(state=NORMAL if selected_format.get() else DISABLED)

    def on_select_audio_convert_format(*args):
        audio_convert_quality_selector.configure(state=NORMAL if audio_convert_format.get() != 'Do not convert' else DISABLED)

    def handle_download():
        nonlocal ydl_opts
        ydl_opts['format'] = selected_format.get()
        if video_only_formats[video_selected_format.get()] is None and audio_only_formats[audio_selected_format.get()] is not None and valid_audio_convert_formats[audio_convert_format.get()] is not None:
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': valid_audio_convert_formats[audio_convert_format.get()], 'preferredquality': audio_convert_quality_values[audio_convert_quality.get()]}]  # 0 highest, 10 lowest.
        # Subtitle options
        selected_sub_lang = subtitle_langs[subtitle_selected.get()]
        if selected_sub_lang:
            is_auto = subtitle_selected.get().endswith('[auto]')
            ydl_opts['writesubtitles'] = not is_auto
            ydl_opts['writeautomaticsub'] = is_auto
            ydl_opts['subtitleslangs'] = [selected_sub_lang]
            ydl_opts['subtitlesformat'] = 'srt/best'
            ydl_opts['translate_captions_tr'] = bool(translate_var.get())
            ydl_opts['dub_turkish'] = bool(dub_var.get())
            ydl_opts['dub_engine'] = dub_engine_var.get()
            ydl_opts['translation_backend'] = 'ollama' if backend_var.get().startswith('Ollama') else 'groq'
            ydl_opts['asr_captions'] = bool(asr_var.get())
        else:
            ydl_opts['translate_captions_tr'] = False
            ydl_opts['dub_turkish'] = False
            ydl_opts['dub_engine'] = dub_engine_var.get() if 'dub_engine_var' in locals() else ydl_opts.get('dub_engine')
            ydl_opts['translation_backend'] = 'ollama' if backend_var.get().startswith('Ollama') else 'groq'
            ydl_opts['asr_captions'] = bool(asr_var.get())
        if cookies_var.get() != 'None':
            ydl_opts['cookiesfrombrowser'] = (cookies_var.get().lower(),)
        if cookies_file_var.get().strip():
            ydl_opts['cookiefile'] = cookies_file_var.get().strip()
        ydl_opts['outtmpl'] = os.path.join(path, ydl_opts['outtmpl'] if isinstance(ydl_opts['outtmpl'], str) else ydl_opts['outtmpl']['default'])
        download_queue.append(DownloadTask(url, path, ydl_opts, queue_frame))
        ydl_opts = ydl_base_opts.copy()  # reset for next task
        details_window.destroy()
        root.lift()

    download_button = Button(scrollableFrame.scrollwindow, text='Download', command=handle_download)  # have to define first else callbacks below complain
    selected_format = StringVar()
    selected_format.trace('w', on_select_format)
    video_only_formats, audio_only_formats = {'Select video format': None}, {'Select audio format': None}  # {text: value} except for None
    radiobutton_frame = Frame(formats_frame, relief='groove', borderwidth=3)
    radiobutton_frame.pack(side=TOP, fill=X, expand=True)
    for f in formats:
        if f['video'] and f['audio']:  # video AND audio
            Radiobutton(radiobutton_frame, variable=selected_format, value=f['format_id'],
                        text=f'Video: {f["video"]["resolution"]}@{f["video"]["fps"]}fps {f["video"]["codec"]} {"HDR" if f["video"]["hdr"] else ""}\n'
                             f'Audio: {f["audio"]["sample_rate"]} {f["audio"]["bitrate"]} {f["audio"]["codec"]}\n'
                             f'File extension: {f["ext"]} Size: ' + (f'{round(f["size"] / (1024 * 1024), 2)}MB' if f["size"] else 'unknown size')).pack(side=TOP, fill=X, expand=True, pady=(0, 10))
        else:  # video only or audio only
            if f['video']: video_only_formats[f'{f["video"]["resolution"]}@{f["video"]["fps"]}fps {f["video"]["codec"]} {"HDR" if f["video"]["hdr"] else ""}'] = f['format_id']
            if f['audio']: audio_only_formats[f'{f["audio"]["sample_rate"]} {f["audio"]["bitrate"]} {f["audio"]["codec"]}'] = f['format_id']
    if not len(radiobutton_frame.winfo_children()): Label(radiobutton_frame, text='No stream containing both video and audio available', anchor=CENTER).pack(side=TOP, fill=X, expand=True)

    custom_formats_frame = LabelFrame(formats_frame, text='Customize', borderwidth=3)
    custom_formats_frame.pack(side=TOP, fill=X, expand=True)
    video_selected_format = StringVar(value='Select video format')
    video_selected_format.trace('w', on_select_single_format)
    audio_selected_format = StringVar(value='Select audio format')
    audio_selected_format.trace('w', on_select_single_format)
    OptionMenu(custom_formats_frame, video_selected_format, 'Select video format', *video_only_formats.keys()).pack(side=TOP, fill=X, expand=True, pady=(0, 10))
    OptionMenu(custom_formats_frame, audio_selected_format, 'Select audio format', *audio_only_formats.keys()).pack(side=TOP, fill=X, expand=True, pady=(0, 10))
    audio_convert_format = StringVar(value='Do not convert')
    valid_audio_convert_formats = {'Do not convert': None, 'AAC (.m4a)': 'aac', 'ALAC (.m4a)': 'alac', 'FLAC (.flac)': 'flac', 'm4a (.m4a)': 'm4a', 'mp3 (.mp3)': 'mp3', 'Opus (.opus)': 'opus', 'Vorbis (.ogg)': 'vorbis', 'WAV (.wav)': 'wav'}
    audio_convert_frame = LabelFrame(formats_frame, text='Convert audio format to', borderwidth=3)
    audio_convert_frame.pack(side=TOP, fill=X, expand=True)
    audio_convert_selector = OptionMenu(audio_convert_frame, audio_convert_format, 'Do not convert', *valid_audio_convert_formats.keys(), command=on_select_audio_convert_format)
    audio_convert_selector.pack(side=TOP, fill=X, expand=True, pady=(5, 5))
    audio_convert_selector.configure(state=DISABLED)
    audio_convert_quality = StringVar(value='5')
    audio_convert_quality_values = {k: v for v, k in enumerate(['0 (Highest Quality)', '1', '2', '3', '4', '5 (Medium Quality)', '6', '7', '8', '9', '10 (Lowest Quality)'])}
    audio_convert_quality_selector = OptionMenu(audio_convert_frame, audio_convert_quality, '5 (Medium Quality)', *audio_convert_quality_values)
    audio_convert_quality_selector.pack(side=TOP, fill=X, expand=True, pady=(5, 5))
    audio_convert_quality_selector.configure(state=DISABLED)

    # Subtitle selection
    subtitles = parsed_info.get('subtitles', {})
    automatic_captions = parsed_info.get('automatic_captions', {})
    subtitle_langs = {'None': None}
    # Prefer manual subtitles
    for lang_code, lang_info in subtitles.items():
        lang_name = lang_info[0].get('name', lang_code) if lang_info else lang_code
        subtitle_langs[f'{lang_name} ({lang_code}) [manual]'] = lang_code
    # Add automatic captions as fallback
    for lang_code, lang_info in automatic_captions.items():
        if lang_code not in subtitles:  # only add if not already available as manual
            lang_name = lang_info[0].get('name', lang_code) if lang_info else lang_code
            subtitle_langs[f'{lang_name} ({lang_code}) [auto]'] = lang_code

    subtitle_frame = LabelFrame(formats_frame, text='Download Subtitles (sidecar, SRT preferred)', borderwidth=3)
    subtitle_frame.pack(side=TOP, fill=X, expand=True, pady=(10, 0))
    auto_var = BooleanVar(value=False)

    def _auto_toggle():
        if auto_var.get():
            keys = [k for k in subtitle_langs.keys() if k != 'None']
            if keys:
                subtitle_selected.set(keys[0])
            else:
                asr_var.set(True)
            translate_var.set(True)
            dub_var.set(True)

    Checkbutton(subtitle_frame, text='Otomatik işle (link yapıştır, gerisini sistem yapsın)', variable=auto_var, command=_auto_toggle).pack(side=TOP, fill=X, expand=True, pady=(0, 2))

    subtitle_selected = StringVar(value='None')
    OptionMenu(subtitle_frame, subtitle_selected, 'None', *subtitle_langs.keys()).pack(side=TOP, fill=X, expand=True, pady=(5, 5))

    translate_var = BooleanVar(value=False)
    translate_check = Checkbutton(subtitle_frame, text='Translate captions to Turkish (Groq API)', variable=translate_var, state=DISABLED)
    translate_check.pack(side=TOP, fill=X, expand=True, pady=(0, 2))
    Label(subtitle_frame, text='Önce bir altyazı seçin; seçenekler aktif olur.', anchor=W, foreground='gray').pack(side=TOP, fill=X, expand=True, pady=(0, 2))

    asr_var = BooleanVar(value=False)
    asr_check = Checkbutton(subtitle_frame, text='Generate captions from audio (Groq Whisper ASR)', variable=asr_var, state=DISABLED)
    asr_check.pack(side=TOP, fill=X, expand=True, pady=(0, 2))

    backend_var = StringVar(value='Ollama Beam Cloud')
    backend_frame = Frame(subtitle_frame)
    backend_frame.pack(side=TOP, fill=X, expand=True, pady=(0, 2))
    Label(backend_frame, text='Translation backend:').pack(side=LEFT)
    OptionMenu(backend_frame, backend_var, 'Groq', 'Ollama Beam Cloud').pack(side=LEFT, fill=X, expand=True)

    cookies_var = StringVar(value='None')
    cookies_frame = Frame(subtitle_frame)
    cookies_frame.pack(side=TOP, fill=X, expand=True, pady=(0, 2))
    Label(cookies_frame, text='Cookies from browser:').pack(side=LEFT)
    OptionMenu(cookies_frame, cookies_var, 'None', 'Chrome', 'Firefox', 'Edge', 'Brave').pack(side=LEFT, fill=X, expand=True)

    cookies_file_frame = Frame(subtitle_frame)
    cookies_file_frame.pack(side=TOP, fill=X, expand=True, pady=(0, 2))
    Label(cookies_file_frame, text='Cookies file (optional):').pack(side=LEFT)
    Entry(cookies_file_frame, textvariable=cookies_file_var).pack(side=LEFT, fill=X, expand=True)

    dub_var = BooleanVar(value=False)
    dub_check = Checkbutton(
        subtitle_frame,
        text='Dub to Turkish (Türkçe dublaj)',
        variable=dub_var,
        state=DISABLED,
    )
    dub_check.pack(side=TOP, fill=X, expand=True, pady=(0, 2))

    dub_engines = get_available_engines()
    dub_engine_var = StringVar(value=dub_engines[0])

    engine_frame = Frame(subtitle_frame)
    engine_frame.pack(side=TOP, fill=X, expand=True, pady=(2, 5))
    Label(engine_frame, text='Motor:').pack(side=LEFT, padx=(5, 5))
    dub_engine_menu = OptionMenu(engine_frame, dub_engine_var, dub_engines[0], *dub_engines)
    dub_engine_menu.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))
    dub_engine_menu.configure(state=DISABLED)

    def _handle_test_voice():
        def _worker():
            eng = dub_engine_var.get()
            try:
                root.after(0, lambda: test_voice_btn.configure(text='Ses Üretiliyor...', state=DISABLED))
                root.after(0, lambda: status(f'Test sesi üretiliyor: {eng}'))
                wav_path = test_voice_sample(eng)
                if sys.platform == 'win32':
                    import winsound
                    winsound.PlaySound(wav_path, winsound.SND_ASYNC | winsound.SND_FILENAME)
                root.after(0, lambda: status(f'Ses testi başarılı: {eng}'))
            except Exception as e:
                err = str(e)
                root.after(0, lambda: status(f'Ses testi hatası: {err[:100]}'))
                root.after(0, lambda: messagebox.showerror('Ses Testi Hatasi', 'Test sesi uretilemedi: ' + err))
            finally:
                root.after(0, lambda: test_voice_btn.configure(text='Sesi Dene', state=NORMAL))

        threading.Thread(target=_worker, daemon=True).start()

    test_voice_btn = Button(engine_frame, text='Sesi Dene', command=_handle_test_voice, state=DISABLED)
    test_voice_btn.pack(side=RIGHT, padx=(0, 5))

    def _sync_translate_state(*_args):
        if subtitle_selected.get() == 'None':
            translate_var.set(False)
            dub_var.set(False)
            translate_check.configure(state=DISABLED if not asr_var.get() else NORMAL)
            dub_check.configure(state=DISABLED)
            dub_engine_menu.configure(state=DISABLED)
            test_voice_btn.configure(state=DISABLED)
            asr_check.configure(state=NORMAL)
            if asr_var.get():
                _sync_dub_state()
        else:
            asr_var.set(False)
            asr_check.configure(state=DISABLED)
            translate_check.configure(state=NORMAL)
            _sync_dub_state()

    def _sync_dub_state(*_args):
        if translate_var.get() or asr_var.get():
            dub_check.configure(state=NORMAL)
            if dub_var.get():
                dub_engine_menu.configure(state=NORMAL)
                test_voice_btn.configure(state=NORMAL)
            else:
                dub_engine_menu.configure(state=DISABLED)
                test_voice_btn.configure(state=DISABLED)
        else:
            dub_var.set(False)
            dub_check.configure(state=DISABLED)
            dub_engine_menu.configure(state=DISABLED)
            test_voice_btn.configure(state=DISABLED)

    def _sync_asr_state(*_args):
        if subtitle_selected.get() == 'None':
            translate_check.configure(state=NORMAL if asr_var.get() else DISABLED)
            if not asr_var.get():
                translate_var.set(False)
            _sync_dub_state()

    subtitle_selected.trace('w', _sync_translate_state)
    translate_var.trace('w', _sync_dub_state)
    asr_var.trace('w', _sync_asr_state)
    dub_var.trace('w', _sync_dub_state)
    _sync_translate_state()

    download_button.pack(side=TOP, fill=X, expand=True, padx=10, pady=(0, 10))  # defined at top
    status('Ready')


def select_save_path():
    path = filedialog.askdirectory(initialdir=initial_dir, title='Choose a folder to save the downloaded file')
    if path:
        path_input.delete(0, END)
        path_input.insert(0, path)
    if sys.platform == 'win32':
        winreg.SetValue(winreg.HKEY_CURRENT_USER, 'Software\\YT-DLP GUI', winreg.REG_SZ, path)
    else:
        os.makedirs('~/.config/yt-dlp', exist_ok=True)
        with open('~/.config/yt-dlp/last_path.txt', 'w+') as f:
            f.write(path)


def do_tasks():
    if download_queue and not ongoing_task:
        task = download_queue[0]
        if task.extract_info_succeed:
            task.start_task()
        else:
            download_queue.remove(task)
            status('Ready')
    queue_frame.after(1000, do_tasks)
    root.update()


initial_dir = os.getcwd()
if sys.platform == 'win32':
    try:
        initial_dir = winreg.QueryValue(winreg.HKEY_CURRENT_USER, 'Software\\YT-DLP GUI')
    except FileNotFoundError:  # if key not found
        pass
elif os.path.isfile('~/.config/yt-dlp/last_path.txt'):
    with open('~/.config/yt-dlp/last_path.txt', 'r') as f:
        initial_dir = f.read()

url_input_frame = Frame(root)
url_input_frame.pack(expand=True, fill=BOTH, side=TOP, pady=(5, 10))
Label(url_input_frame, text='URL: ').pack(side=LEFT, padx=(10, 0))
url_input = Entry(url_input_frame, width=50)
url_input.pack(expand=True, fill=X, side=LEFT, padx=(2, 10))
url_input.focus_set()

path_input_frame = Frame(root)
path_input_frame.pack(expand=True, fill=BOTH, side=TOP, pady=(5, 10))
Label(path_input_frame, text='Path: ').pack(side=LEFT, padx=(10, 0))
path_input = Entry(path_input_frame, width=50)
path_input.pack(expand=True, fill=X, side=LEFT, padx=(2, 0))
path_input.insert(0, initial_dir.replace('\\', '/'))
path_select_button = Button(path_input_frame, text='Choose', command=select_save_path)
path_select_button.pack(side=LEFT, padx=(2, 10))

buttons_frame = Frame(root)
buttons_frame.pack(expand=True, fill=BOTH, side=TOP, anchor=NW)
download_video_button = Button(buttons_frame, text='Download Video', command=lambda: handle_download_video_best(url_input.get(), path_input.get()))
download_video_button.pack(expand=True, fill=X, side=LEFT, padx=(10, 0))
download_audio_button = Button(buttons_frame, text='Download Audio', command=lambda: handle_download_audio_best(url_input.get(), path_input.get()))
download_audio_button.pack(expand=True, fill=X, side=LEFT, padx=(2, 0))
download_info_button = Button(buttons_frame, text='Customize Downloads', command=lambda: handle_download_info(url_input.get(), path_input.get()))
download_info_button.pack(expand=True, fill=X, side=LEFT, padx=(2, 10))

scroll_container_frame = Frame(root)
scroll_container_frame.pack(expand=True, fill=BOTH, side=TOP)
scrollableFrame = ScrolledWindow(scroll_container_frame)
queue_frame = LabelFrame(scrollableFrame.scrollwindow, text='Download Queue')
queue_frame.pack(fill=BOTH, expand=True, anchor=CENTER, padx=(10, 10), pady=(10, 0))
do_tasks()

status('Ready')
root.mainloop()
