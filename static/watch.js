/** Theater Watch view controller: range playback, server+local resume, auto-hiding HUD. */

import { ChatController } from '/chat.js';

const LOCAL_POS_KEY = 'offline_player_positions';

export class WatchController {
  constructor({ onExit, onOpenPairModal, onProgressSaved, showToast }) {
    this.onExit = onExit;
    this.onOpenPairModal = onOpenPairModal;
    this.onProgressSaved = onProgressSaved;
    this.showToast = showToast;

    this.viewEl = document.getElementById('watch-view');
    this.stageEl = document.getElementById('watch-stage');
    this.hudEl = document.getElementById('watch-hud');
    this.player = document.getElementById('video-player');
    this.titleEl = document.getElementById('watch-title');
    this.metaEl = document.getElementById('watch-meta');
    this.speedSelect = document.getElementById('speed-select');
    this.chatLabelEl = document.getElementById('hud-chat-label');
    this.toggleChatBtn = document.getElementById('btn-hud-toggle-chat');
    this.chatSidebar = document.getElementById('chat-sidebar');
    this.resizerEl = document.getElementById('watch-resizer');

    this.chat = new ChatController({
      containerEl: document.getElementById('chat-messages'),
      countEl: document.getElementById('chat-count-label'),
      pausedPillEl: document.getElementById('chat-paused-pill'),
      imagesBtnEl: document.getElementById('btn-hud-images'),
      imagesLabelEl: document.getElementById('hud-images-label'),
      onSeek: (sec) => {
        this.player.currentTime = sec;
      },
    });

    this.activeVideo = null;
    this.activeLocalKey = null;
    this.objectUrl = null;
    this.isRestoring = false;
    this.pendingRestoreTime = 0;
    this.restoreFallbackTimer = null;
    this.autoPlayPending = false;
    this.lastSavedWall = 0;
    this.hudHideTimer = null;
    this.isActive = false;

    this._bindEvents();
    this._restoreSidebarWidth();
  }

  _bindEvents() {
    document.getElementById('btn-back-library').addEventListener('click', () => this.close());
    document.getElementById('btn-hud-images').addEventListener('click', () => this.chat.toggleExternalImages());
    document.getElementById('btn-hud-pair-chat').addEventListener('click', () => {
      if (this.activeVideo) {
        this.onOpenPairModal(this.activeVideo);
      } else {
        document.getElementById('local-chat-input').click();
      }
    });
    this.toggleChatBtn.addEventListener('click', () => this.toggleChat());

    this.speedSelect.addEventListener('change', () => {
      this.player.playbackRate = parseFloat(this.speedSelect.value) || 1;
    });

    // Auto-hide HUD on mouse idle
    this.stageEl.addEventListener('mousemove', () => this.showHudTemporarily());
    this.stageEl.addEventListener('mouseenter', () => this.showHudTemporarily());
    this.stageEl.addEventListener('mouseleave', () => this.hideHudNow());
    this.hudEl.addEventListener('mouseleave', () => this.showHudTemporarily());

    // Resizable chat sidebar
    let dragging = false;
    this.resizerEl.addEventListener('mousedown', () => {
      dragging = true;
      this.resizerEl.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });
    document.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const totalWidth = this.viewEl.clientWidth;
      const nextWidth = totalWidth - e.clientX;
      if (nextWidth >= 250 && nextWidth <= totalWidth * 0.65) {
        this.chatSidebar.style.width = `${nextWidth}px`;
        localStorage.setItem('yt_chat_width', String(nextWidth));
      }
    });
    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      this.resizerEl.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    });

    // Media events & robust seekable resume
    this.player.addEventListener('loadedmetadata', () => {
      if (this.isRestoring) this._tryRestorePosition();
      else this._startAutoPlayIfPending();
    });
    this.player.addEventListener('loadeddata', () => {
      if (this.isRestoring) this._tryRestorePosition();
    });
    this.player.addEventListener('canplay', () => {
      if (this.isRestoring) {
        this._tryRestorePosition();
        if (!this.restoreFallbackTimer) {
          const expectedId = this.activeVideo?.id || this.activeLocalKey;
          this.restoreFallbackTimer = setTimeout(() => {
            this.restoreFallbackTimer = null;
            const currentId = this.activeVideo?.id || this.activeLocalKey;
            if (!this.isRestoring || currentId !== expectedId) return;
            if (this.pendingRestoreTime > 0 && Math.abs(this.player.currentTime - this.pendingRestoreTime) > 12) {
              try { this.player.currentTime = this.pendingRestoreTime; } catch (_) {}
            }
            this.isRestoring = false;
            this.pendingRestoreTime = 0;
            this.chat.resync(this.player.currentTime);
            this._startAutoPlayIfPending();
          }, 800);
        }
      } else {
        this._startAutoPlayIfPending();
      }
    });

    this.player.addEventListener('seeked', () => {
      if (this.isRestoring) {
        if (Math.abs(this.player.currentTime - this.pendingRestoreTime) <= 12) {
          if (this.restoreFallbackTimer) {
            clearTimeout(this.restoreFallbackTimer);
            this.restoreFallbackTimer = null;
          }
          this.isRestoring = false;
          this.pendingRestoreTime = 0;
          this.chat.resync(this.player.currentTime);
          this._startAutoPlayIfPending();
        } else {
          this._tryRestorePosition();
        }
        return;
      }
      this.chat.resync(this.player.currentTime);
      this.saveProgress(true);
    });

    this.player.addEventListener('timeupdate', () => {
      this.saveProgress(false);
      this.chat.onTimeUpdate(this.player.currentTime);
    });

    this.player.addEventListener('pause', () => this.saveProgress(true));
    this.player.addEventListener('ended', () => this._clearProgress());
    window.addEventListener('pagehide', () => this.saveProgress(true));

    document.addEventListener('keydown', (e) => {
      if (!this.isActive) return;
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) return;

      switch (e.key.toLowerCase()) {
        case ' ':
        case 'k':
          e.preventDefault();
          if (this.player.paused) this.player.play();
          else this.player.pause();
          break;
        case 'arrowleft':
          e.preventDefault();
          this.player.currentTime = Math.max(0, this.player.currentTime - 5);
          break;
        case 'arrowright':
          e.preventDefault();
          this.player.currentTime = Math.min(this.player.duration || Infinity, this.player.currentTime + 5);
          break;
        case 'j':
          e.preventDefault();
          this.player.currentTime = Math.max(0, this.player.currentTime - 10);
          break;
        case 'l':
          e.preventDefault();
          this.player.currentTime = Math.min(this.player.duration || Infinity, this.player.currentTime + 10);
          break;
        case 'm':
          this.player.muted = !this.player.muted;
          break;
        case 'f':
          e.preventDefault();
          if (!document.fullscreenElement) this.viewEl.requestFullscreen().catch(() => {});
          else document.exitFullscreen().catch(() => {});
          break;
        case 'c':
          this.toggleChat();
          break;
        case '[':
          this._stepSpeed(-1);
          break;
        case ']':
          this._stepSpeed(1);
          break;
      }
    });
  }

  _stepSpeed(delta) {
    const opts = Array.from(this.speedSelect.options);
    const idx = Math.max(0, Math.min(opts.length - 1, this.speedSelect.selectedIndex + delta));
    this.speedSelect.selectedIndex = idx;
    this.player.playbackRate = parseFloat(opts[idx].value) || 1;
    this.showToast(`Playback speed: ${opts[idx].textContent}`);
  }

  _restoreSidebarWidth() {
    const saved = localStorage.getItem('yt_chat_width');
    if (saved) this.chatSidebar.style.width = `${saved}px`;
  }

  showHudTemporarily() {
    if (!this.isActive) return;
    this.stageEl.classList.add('hud-visible');
    if (this.hudHideTimer) clearTimeout(this.hudHideTimer);
    this.hudHideTimer = setTimeout(() => this.hideHudNow(), 2200);
  }

  hideHudNow() {
    if (this.hudHideTimer) {
      clearTimeout(this.hudHideTimer);
      this.hudHideTimer = null;
    }
    if (this.hudEl.matches(':hover')) return;
    this.stageEl.classList.remove('hud-visible');
  }

  toggleChat(forceShow = null) {
    const currentlyHidden = this.chatSidebar.classList.contains('collapsed');
    const show = forceShow === null ? currentlyHidden : forceShow;
    this.chatSidebar.classList.toggle('collapsed', !show);
    this.resizerEl.classList.toggle('collapsed', !show);
    this.toggleChatBtn.classList.toggle('active', show);
  }

  _startAutoPlayIfPending() {
    if (this.autoPlayPending) {
      this.autoPlayPending = false;
      this.player.play().catch(() => {});
    }
  }

  _tryRestorePosition() {
    if (!this.isRestoring || this.pendingRestoreTime <= 0) return false;
    if (isFinite(this.player.duration) && this.player.duration > 0 && this.pendingRestoreTime >= this.player.duration - 5) {
      this.isRestoring = false;
      this.pendingRestoreTime = 0;
      return false;
    }
    if (!this.player.seekable || this.player.seekable.length === 0) {
      return false;
    }
    if (Math.abs(this.player.currentTime - this.pendingRestoreTime) > 2) {
      this.player.currentTime = this.pendingRestoreTime;
    }
    return true;
  }

  _getLocalResume(key) {
    try {
      const map = JSON.parse(localStorage.getItem(LOCAL_POS_KEY) || '{}');
      const entry = map[key];
      return entry && typeof entry.t === 'number' ? entry.t : 0;
    } catch (_) {
      return 0;
    }
  }

  _setLocalResume(key, time) {
    try {
      const map = JSON.parse(localStorage.getItem(LOCAL_POS_KEY) || '{}');
      map[key] = { t: Math.round(time * 10) / 10, u: Date.now() };
      localStorage.setItem(LOCAL_POS_KEY, JSON.stringify(map));
    } catch (_) {}
  }

  _clearProgress() {
    if (this.activeVideo) {
      fetch(`/api/history?id=${encodeURIComponent(this.activeVideo.id)}`, { method: 'DELETE' }).catch(() => {});
      this.onProgressSaved(this.activeVideo.id, 0);
    }
    if (this.activeLocalKey) {
      try {
        const map = JSON.parse(localStorage.getItem(LOCAL_POS_KEY) || '{}');
        delete map[this.activeLocalKey];
        localStorage.setItem(LOCAL_POS_KEY, JSON.stringify(map));
      } catch (_) {}
    }
  }

  saveProgress(force = false) {
    if (!this.isActive || this.isRestoring) return;
    const now = Date.now();
    if (!force && now - this.lastSavedWall < 3000) return;

    const pos = this.player.currentTime;
    const dur = isFinite(this.player.duration) ? this.player.duration : 0;
    if (!isFinite(pos) || pos < 5) return;

    this.lastSavedWall = now;
    if (dur > 0 && pos >= dur - 5) {
      this._clearProgress();
      return;
    }

    if (this.activeVideo) {
      this.onProgressSaved(this.activeVideo.id, pos);
      fetch('/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_id: this.activeVideo.id, position: pos, duration: dur }),
      }).catch(() => {});
    } else if (this.activeLocalKey) {
      this._setLocalResume(this.activeLocalKey, pos);
    }
  }

  async openLibraryVideo(video) {
    this._resetSource();
    this.activeVideo = video;
    this.activeLocalKey = null;
    this.isActive = true;

    this.pendingRestoreTime = video.progress?.position || 0;
    this.isRestoring = this.pendingRestoreTime > 0;
    this.autoPlayPending = true;

    this.titleEl.textContent = video.title || video.name;
    const parts = [];
    if (video.height) parts.push(`${video.height}p${video.fps ? video.fps : ''}`);
    if (video.vcodec) parts.push(video.vcodec.toUpperCase());
    this.metaEl.textContent = parts.join(' · ');

    this.player.src = `/stream?id=${encodeURIComponent(video.id)}`;
    this.player.playbackRate = parseFloat(this.speedSelect.value) || 1;
    this.viewEl.classList.add('active');
    this.showHudTemporarily();

    if (video.chat) {
      this.chatLabelEl.textContent = 'Chat Paired';
      try {
        await this.chat.loadFromServer(video.chat.id, () => this.player.currentTime || 0);
      } catch (err) {
        this.showToast(`Chat load error: ${err.message}`);
        this.chat.clear();
      }
    } else {
      this.chatLabelEl.textContent = 'Attach Chat';
      this.chat.clear();
    }
  }

  openLocalVideoFile(file) {
    this._resetSource();
    this.activeVideo = null;
    this.activeLocalKey = `${file.name}:${file.size}`;
    this.isActive = true;

    this.pendingRestoreTime = this._getLocalResume(this.activeLocalKey);
    this.isRestoring = this.pendingRestoreTime > 0;
    this.autoPlayPending = true;

    this.titleEl.textContent = file.name;
    this.metaEl.textContent = 'Local File';
    this.chatLabelEl.textContent = 'Load Chat';

    this.objectUrl = URL.createObjectURL(file);
    this.player.src = this.objectUrl;
    this.viewEl.classList.add('active');
    this.showHudTemporarily();
    this.chat.clear();
  }

  async openLocalChatFile(file) {
    if (!this.isActive) return;
    this.chatLabelEl.textContent = 'Local Chat';
    await this.chat.loadFromLocalFile(file, () => this.player.currentTime || 0);
    this.showToast(`Loaded ${file.name}`);
  }

  async refreshPairedChat(video) {
    this.activeVideo = video;
    if (video.chat) {
      this.chatLabelEl.textContent = 'Chat Paired';
      await this.chat.loadFromServer(video.chat.id, () => this.player.currentTime || 0);
    } else {
      this.chatLabelEl.textContent = 'Attach Chat';
      this.chat.clear();
    }
  }

  _resetSource() {
    if (this.isActive) this.saveProgress(true);
    if (this.restoreFallbackTimer) {
      clearTimeout(this.restoreFallbackTimer);
      this.restoreFallbackTimer = null;
    }
    if (this.objectUrl) {
      URL.revokeObjectURL(this.objectUrl);
      this.objectUrl = null;
    }
    this.player.pause();
    this.player.removeAttribute('src');
    this.player.load();
    this.lastSavedWall = 0;
  }

  close() {
    this._resetSource();
    this.isActive = false;
    this.activeVideo = null;
    this.viewEl.classList.remove('active');
    this.onExit();
  }
}
