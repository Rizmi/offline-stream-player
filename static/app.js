/** Main SPA Dashboard controller: YouTube-style library, downloads, chat merge, settings. */

import { formatTime } from '/chat.js';
import { WatchController } from '/watch.js';

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let val = bytes;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(i >= 2 ? 1 : 0)} ${units[i]}`;
}

class StreamVaultApp {
  constructor() {
    this.videos = [];
    this.chats = [];
    this.settings = null;
    this.jobs = [];
    this.activeFilter = 'all';
    this.sortBy = 'newest';
    this.searchQuery = '';
    this.openDropdownId = null;
    this.pairingVideo = null;
    this.thumbPollTimer = null;
    this.queuePollTimer = null;
    this.lastJobStates = new Map();

    this.libraryView = document.getElementById('library-view');
    this.videoGrid = document.getElementById('video-grid');
    this.continueShelf = document.getElementById('continue-shelf');
    this.continueGrid = document.getElementById('continue-grid');
    this.emptyState = document.getElementById('empty-library');
    this.searchInput = document.getElementById('search-input');
    this.sortSelect = document.getElementById('sort-select');
    this.queueBadge = document.getElementById('queue-badge');
    this.toastEl = document.getElementById('toast');

    this.watch = new WatchController({
      onExit: () => {
        this.libraryView.classList.add('active');
        window.history.replaceState(null, '', '#');
        this.renderLibrary();
      },
      onOpenPairModal: (video) => this.openPairModal(video),
      onProgressSaved: (vid, pos) => {
        const target = this.videos.find((v) => v.id === vid);
        if (target) {
          target.progress = {
            ...target.progress,
            position: pos,
            updated_at: pos > 0 ? Date.now() : 0,
          };
        }
      },
      showToast: (msg) => this.showToast(msg),
    });

    this._bindEvents();
    this.init();
  }

  _bindEvents() {
    this.searchInput.addEventListener('input', () => {
      this.searchQuery = this.searchInput.value.trim().toLowerCase();
      this.renderLibrary();
    });

    this.sortSelect.addEventListener('change', () => {
      this.sortBy = this.sortSelect.value;
      this.renderLibrary();
    });

    document.getElementById('filter-chips').addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      document.querySelectorAll('#filter-chips .chip').forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      this.activeFilter = chip.dataset.filter;
      this.renderLibrary();
    });

    document.getElementById('btn-rescan').addEventListener('click', () => this.fetchLibrary(true));
    document.getElementById('btn-new-download').addEventListener('click', () => this.openDownloadModal());
    document.getElementById('btn-downloads-drawer').addEventListener('click', () => this.openModal('queue-drawer'));
    document.getElementById('btn-settings').addEventListener('click', () => this.openSettingsModal());
    document.getElementById('btn-merge-chats').addEventListener('click', () => this.openMergeModal());
    document.getElementById('btn-open-local').addEventListener('click', () => document.getElementById('local-video-input').click());

    // Local file fallback
    document.getElementById('local-video-input').addEventListener('change', (e) => {
      const file = e.target.files?.[0];
      if (file) {
        this.libraryView.classList.remove('active');
        this.watch.openLocalVideoFile(file);
      }
      e.target.value = '';
    });
    document.getElementById('local-chat-input').addEventListener('change', (e) => {
      const file = e.target.files?.[0];
      if (file) this.watch.openLocalChatFile(file);
      e.target.value = '';
    });

    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => {
      e.preventDefault();
      for (const file of e.dataTransfer?.files || []) {
        if (/\.(mkv|mp4|webm|m4v|mov)$/i.test(file.name)) {
          this.libraryView.classList.remove('active');
          this.watch.openLocalVideoFile(file);
        } else if (file.name.toLowerCase().endsWith('.json')) {
          if (this.watch.isActive) this.watch.openLocalChatFile(file);
        }
      }
    });

    // Modal closing
    document.querySelectorAll('[data-close-modal]').forEach((btn) => {
      btn.addEventListener('click', () => this.closeModal(btn.dataset.closeModal));
    });
    document.querySelectorAll('.modal-backdrop, .drawer-backdrop').forEach((bd) => {
      bd.addEventListener('click', (e) => {
        if (e.target === bd) this.closeModal(bd.id);
      });
    });

    // Global dropdown / keyboard shortcuts
    document.addEventListener('click', (e) => {
      if (this.openDropdownId && !e.target.closest('.card-dropdown') && !e.target.closest('.card-menu-btn')) {
        this.openDropdownId = null;
        document.querySelectorAll('.card-dropdown').forEach((d) => d.classList.add('hidden'));
      }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && !this.watch.isActive && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
        e.preventDefault();
        this.searchInput.focus();
      } else if (e.key === 'Escape') {
        document.querySelectorAll('.modal-backdrop.open, .drawer-backdrop.open').forEach((m) => m.classList.remove('open'));
      }
    });

    // Download modal controls
    document.getElementById('btn-probe-url').addEventListener('click', () => this.probeDownloadUrl());
    document.getElementById('dl-mode').addEventListener('change', () => this._syncDownloadModalFields());
    document.getElementById('btn-start-download').addEventListener('click', () => this.submitDownload());

    // Pair modal
    document.getElementById('btn-save-pair').addEventListener('click', () => this.savePairModal(false));
    document.getElementById('btn-unpair-chat').addEventListener('click', () => this.savePairModal(true));

    // Merge modal
    document.getElementById('btn-run-merge').addEventListener('click', () => this.runMergeChats());

    // Settings modal
    document.getElementById('btn-save-settings').addEventListener('click', () => this.saveSettings());
  }

  async init() {
    await Promise.all([this.fetchSettings(), this.fetchLibrary(false), this.fetchDownloads()]);
    this._startQueuePolling();

    const hash = window.location.hash;
    if (hash.startsWith('#watch/')) {
      const vid = hash.slice('#watch/'.length);
      const match = this.videos.find((v) => v.id === vid);
      if (match) this.openWatch(match);
    }
  }

  showToast(msg) {
    this.toastEl.textContent = msg;
    this.toastEl.classList.remove('hidden');
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => this.toastEl.classList.add('hidden'), 3000);
  }

  openModal(id) {
    document.getElementById(id).classList.add('open');
  }

  closeModal(id) {
    document.getElementById(id).classList.remove('open');
  }

  navigateHome() {
    if (this.watch.isActive) this.watch.close();
  }

  async fetchSettings() {
    try {
      const res = await fetch('/api/settings');
      this.settings = await res.json();
      document.getElementById('library-dir-chip').textContent = this.settings.download_dir;
      document.getElementById('dl-dest-hint').textContent = `Saves to ${this.settings.download_dir}`;
    } catch (_) {}
  }

  async fetchLibrary(showToastOnDone = false) {
    try {
      const res = await fetch('/api/library?rescan=1');
      const data = await res.json();
      this.videos = data.videos || [];
      this.chats = data.chats || [];
      if (data.download_dir) {
        document.getElementById('library-dir-chip').textContent = data.download_dir;
      }
      this.renderLibrary();
      this._scheduleThumbRefreshIfPending();
      if (showToastOnDone) this.showToast(`Library synced (${this.videos.length} videos)`);
    } catch (err) {
      this.showToast(`Library scan failed: ${err.message}`);
    }
  }

  _scheduleThumbRefreshIfPending() {
    clearTimeout(this.thumbPollTimer);
    if (this.videos.some((v) => !v.has_thumb)) {
      this.thumbPollTimer = setTimeout(() => this.fetchLibrary(false), 2500);
    }
  }

  _filteredVideos() {
    let list = this.videos.filter((v) => {
      if (this.searchQuery) {
        const hay = `${v.title} ${v.name}`.toLowerCase();
        if (!hay.includes(this.searchQuery)) return false;
      }
      if (this.activeFilter === 'with_chat') return Boolean(v.chat);
      if (this.activeFilter === 'no_chat') return !v.chat;
      if (this.activeFilter === 'in_progress') return (v.progress?.position || 0) >= 5;
      return true;
    });

    list = [...list];
    if (this.sortBy === 'name') list.sort((a, b) => a.title.localeCompare(b.title));
    else if (this.sortBy === 'duration') list.sort((a, b) => (b.duration || 0) - (a.duration || 0));
    else if (this.sortBy === 'size') list.sort((a, b) => (b.size || 0) - (a.size || 0));
    else list.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
    return list;
  }

  renderLibrary() {
    // Continue watching shelf
    const resumable = this.videos
      .filter((v) => (v.progress?.position || 0) >= 5)
      .sort((a, b) => (b.progress?.updated_at || 0) - (a.progress?.updated_at || 0))
      .slice(0, 6);

    if (resumable.length && !this.searchQuery && this.activeFilter === 'all') {
      this.continueShelf.classList.remove('hidden');
      document.getElementById('continue-count').textContent = `${resumable.length} active`;
      this.continueGrid.innerHTML = '';
      for (const v of resumable) {
        this.continueGrid.appendChild(this._buildVideoCard(v, true));
      }
    } else {
      this.continueShelf.classList.add('hidden');
      this.continueGrid.innerHTML = '';
    }

    const items = this._filteredVideos();
    document.getElementById('library-stats').textContent = `${items.length} of ${this.videos.length} streams`;
    this.videoGrid.innerHTML = '';

    if (!items.length) {
      this.emptyState.classList.remove('hidden');
      return;
    }
    this.emptyState.classList.add('hidden');
    for (const v of items) {
      this.videoGrid.appendChild(this._buildVideoCard(v, false));
    }
  }

  _buildVideoCard(video, compact = false) {
    const card = document.createElement('article');
    card.className = 'video-card';

    const thumbWrap = document.createElement('div');
    thumbWrap.className = 'thumb-wrap';
    thumbWrap.onclick = () => this.openWatch(video);

    if (video.has_thumb) {
      const img = document.createElement('img');
      img.className = 'thumb-img';
      img.loading = 'lazy';
      img.src = `${video.thumb_url}&v=${Math.floor(video.mtime || 0)}`;
      img.alt = video.title;
      thumbWrap.appendChild(img);
    } else {
      const fb = document.createElement('div');
      fb.className = 'thumb-fallback';
      fb.innerHTML = '<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="4" width="20" height="16" rx="2"/><polygon points="10 9 15 12 10 15 10 9"/></svg>';
      thumbWrap.appendChild(fb);
    }

    const overlay = document.createElement('div');
    overlay.className = 'thumb-overlay';
    overlay.innerHTML = '<div class="play-circle"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M8 5.14v14l11-7-11-7z"/></svg></div>';
    thumbWrap.appendChild(overlay);

    if (video.duration > 0) {
      const durBadge = document.createElement('span');
      durBadge.className = 'duration-badge';
      durBadge.textContent = formatTime(video.duration);
      thumbWrap.appendChild(durBadge);
    }

    const pos = video.progress?.position || 0;
    const totalDur = video.duration || video.progress?.duration || 0;
    if (pos >= 5 && totalDur > 0) {
      const track = document.createElement('div');
      track.className = 'card-progress-track';
      const fill = document.createElement('div');
      fill.className = 'card-progress-fill';
      fill.style.width = `${Math.min(100, Math.max(2, (pos / totalDur) * 100))}%`;
      track.appendChild(fill);
      thumbWrap.appendChild(track);
    }

    card.appendChild(thumbWrap);

    const body = document.createElement('div');
    body.className = 'card-body';

    const info = document.createElement('div');
    info.className = 'card-info';
    info.onclick = () => this.openWatch(video);

    const titleEl = document.createElement('div');
    titleEl.className = 'card-title';
    titleEl.textContent = video.title;
    titleEl.title = video.name;
    info.appendChild(titleEl);

    const meta = document.createElement('div');
    meta.className = 'card-meta';
    if (compact && pos >= 5) {
      const p = document.createElement('span');
      p.className = 'meta-pill';
      p.textContent = `At ${formatTime(pos)}`;
      meta.appendChild(p);
    }
    if (video.height) {
      const res = document.createElement('span');
      res.className = 'meta-pill';
      res.textContent = `${video.height}p${video.fps ? video.fps : ''}`;
      meta.appendChild(res);
    }
    if (video.vcodec && !compact) {
      const cod = document.createElement('span');
      cod.className = 'meta-pill';
      cod.textContent = video.vcodec.toUpperCase();
      meta.appendChild(cod);
    }
    const sz = document.createElement('span');
    sz.textContent = formatBytes(video.size);
    meta.appendChild(sz);

    if (video.chat) {
      const chatPill = document.createElement('span');
      chatPill.className = 'meta-pill chat-ok';
      chatPill.textContent = 'Chat';
      meta.appendChild(chatPill);
    }

    info.appendChild(meta);
    body.appendChild(info);

    if (!compact) {
      const menuBtn = document.createElement('button');
      menuBtn.className = 'card-menu-btn';
      menuBtn.title = 'More options';
      menuBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>';

      const dropdown = document.createElement('div');
      dropdown.className = 'card-dropdown hidden';
      dropdown.innerHTML = `
        <button class="dropdown-item" data-act="pair">Pair Chat File</button>
        <button class="dropdown-item" data-act="dl-chat">Download Chat for Video</button>
        <button class="dropdown-item danger" data-act="delete">Delete Media</button>
      `;

      menuBtn.onclick = (e) => {
        e.stopPropagation();
        const wasHidden = dropdown.classList.contains('hidden');
        document.querySelectorAll('.card-dropdown').forEach((d) => d.classList.add('hidden'));
        if (wasHidden) {
          dropdown.classList.remove('hidden');
          this.openDropdownId = video.id;
        } else {
          this.openDropdownId = null;
        }
      };

      dropdown.onclick = (e) => {
        e.stopPropagation();
        const act = e.target.dataset.act;
        dropdown.classList.add('hidden');
        this.openDropdownId = null;
        if (act === 'pair') this.openPairModal(video);
        else if (act === 'dl-chat') this.openDownloadModal({ mode: 'chat_only', targetVideoId: video.id });
        else if (act === 'delete') this.deleteVideo(video);
      };

      body.appendChild(menuBtn);
      card.appendChild(dropdown);
    }

    card.appendChild(body);
    return card;
  }

  openWatch(video) {
    this.libraryView.classList.remove('active');
    window.history.replaceState(null, '', `#watch/${video.id}`);
    this.watch.openLibraryVideo(video);
  }

  async deleteVideo(video) {
    const alsoChat = video.chat ? confirm(`Delete "${video.title}" and its paired chat file?`) : confirm(`Delete "${video.title}"?`);
    if (!alsoChat) return;
    const res = await fetch(`/api/media?id=${encodeURIComponent(video.id)}&delete_chat=${video.chat ? '1' : '0'}`, { method: 'DELETE' });
    if (res.ok) {
      this.showToast('Deleted from library');
      this.fetchLibrary(false);
    } else {
      this.showToast('Delete failed');
    }
  }

  // ---------------- Download Modal & Queue ----------------
  openDownloadModal(opts = {}) {
    document.getElementById('dl-url').value = opts.url || '';
    document.getElementById('dl-mode').value = opts.mode || 'video_chat';
    document.getElementById('dl-format').value = this.settings?.yt_format || 'mkv';
    const qMatch = (this.settings?.yt_quality || '').match(/height<=(\d+)/);
    const dlQSel = document.getElementById('dl-quality');
    dlQSel.value = qMatch && Array.from(dlQSel.options).some((o) => o.value === qMatch[1]) ? qMatch[1] : 'best';
    document.getElementById('probe-preview').classList.add('hidden');
    document.getElementById('dl-error').classList.add('hidden');

    const targetSelect = document.getElementById('dl-target-video');
    targetSelect.innerHTML = '<option value="">Save standalone chat file</option>';
    for (const v of this.videos) {
      const opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = v.title;
      targetSelect.appendChild(opt);
    }
    targetSelect.value = opts.targetVideoId || '';
    this._syncDownloadModalFields();
    this.openModal('download-modal');
    setTimeout(() => document.getElementById('dl-url').focus(), 50);
  }

  _syncDownloadModalFields() {
    const mode = document.getElementById('dl-mode').value;
    const isChatOnly = mode === 'chat_only';
    document.getElementById('dl-quality-group').classList.toggle('hidden', isChatOnly);
    document.getElementById('dl-format-group').classList.toggle('hidden', isChatOnly);
    document.getElementById('dl-target-group').classList.toggle('hidden', !isChatOnly);
  }

  async probeDownloadUrl() {
    const url = document.getElementById('dl-url').value.trim();
    const errEl = document.getElementById('dl-error');
    const preview = document.getElementById('probe-preview');
    errEl.classList.add('hidden');
    if (!url) return;

    const btn = document.getElementById('btn-probe-url');
    btn.disabled = true;
    btn.textContent = 'Checking...';
    try {
      const res = await fetch('/api/probe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Probe failed');

      document.getElementById('probe-thumb').src = data.thumbnail || '';
      document.getElementById('probe-platform').textContent = `${data.platform.toUpperCase()} · ${data.has_live_chat ? 'Chat Available' : 'No Live Chat Found'}`;
      document.getElementById('probe-title').textContent = data.title;
      document.getElementById('probe-meta').textContent = [data.uploader, data.duration ? formatTime(data.duration) : ''].filter(Boolean).join(' · ');
      preview.classList.remove('hidden');

      const qSel = document.getElementById('dl-quality');
      qSel.innerHTML = '<option value="best">Best Available</option>';
      for (const h of data.qualities || []) {
        const opt = document.createElement('option');
        opt.value = String(h);
        opt.textContent = `${h}p`;
        qSel.appendChild(opt);
      }
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove('hidden');
      preview.classList.add('hidden');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Fetch Info';
    }
  }

  async submitDownload() {
    const url = document.getElementById('dl-url').value.trim();
    const mode = document.getElementById('dl-mode').value;
    const quality = document.getElementById('dl-quality').value;
    const format = document.getElementById('dl-format').value;
    const targetVideoId = document.getElementById('dl-target-video').value || null;
    const title = document.getElementById('probe-title').textContent || '';
    const errEl = document.getElementById('dl-error');

    errEl.classList.add('hidden');
    if (!url) {
      errEl.textContent = 'Please paste a YouTube or Kick URL.';
      errEl.classList.remove('hidden');
      return;
    }

    const res = await fetch('/api/downloads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, mode, quality, format, title, target_video_id: targetVideoId }),
    });
    const data = await res.json();
    if (!res.ok) {
      errEl.textContent = data.error || 'Failed to enqueue download';
      errEl.classList.remove('hidden');
      return;
    }
    this.closeModal('download-modal');
    this.openModal('queue-drawer');
    this.fetchDownloads();
    this.showToast('Download queued');
  }

  async fetchDownloads() {
    try {
      const res = await fetch('/api/downloads');
      const data = await res.json();
      this.jobs = data.jobs || [];

      let completedNow = false;
      for (const job of this.jobs) {
        const prev = this.lastJobStates.get(job.id);
        if (prev && prev !== 'completed' && job.status === 'completed') {
          completedNow = true;
        }
        this.lastJobStates.set(job.id, job.status);
      }
      if (completedNow) {
        this.fetchLibrary(false);
        this.showToast('Download completed and added to library');
      }

      const activeCount = this.jobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
      this.queueBadge.textContent = String(activeCount);
      this.queueBadge.classList.toggle('hidden', activeCount === 0);
      this.renderQueue();
    } catch (_) {}
  }

  _startQueuePolling() {
    this.queuePollTimer = setInterval(() => {
      const hasActive = this.jobs.some((j) => j.status === 'queued' || j.status === 'running');
      const drawerOpen = document.getElementById('queue-drawer').classList.contains('open');
      if (hasActive || drawerOpen) {
        this.fetchDownloads();
      }
    }, 1200);
  }

  renderQueue() {
    const listEl = document.getElementById('queue-list');
    if (!this.jobs.length) {
      listEl.innerHTML = '<div class="chat-empty">No downloads in queue yet.</div>';
      return;
    }
    listEl.innerHTML = '';
    for (const job of this.jobs) {
      const card = document.createElement('div');
      card.className = 'job-card';

      const head = document.createElement('div');
      head.className = 'job-head';
      const title = document.createElement('div');
      title.className = 'job-title';
      title.textContent = job.title || job.url;
      const status = document.createElement('span');
      status.className = `job-status ${job.status}`;
      status.textContent = job.status.toUpperCase();
      head.appendChild(title);
      head.appendChild(status);
      card.appendChild(head);

      const bar = document.createElement('div');
      bar.className = 'job-bar-track';
      const fill = document.createElement('div');
      fill.className = 'job-bar-fill';
      fill.style.width = `${job.progress || 0}%`;
      bar.appendChild(fill);
      card.appendChild(bar);

      const meta = document.createElement('div');
      meta.className = 'job-meta';
      const leftText = [job.phase, job.speed, job.eta ? `ETA ${job.eta}` : ''].filter(Boolean).join(' · ');
      const rightText = `${(job.progress || 0).toFixed(1)}%`;
      meta.innerHTML = `<span>${leftText}</span><span>${rightText}</span>`;
      card.appendChild(meta);

      if (job.error) {
        const err = document.createElement('div');
        err.className = 'form-error';
        err.textContent = job.error;
        card.appendChild(err);
      }

      if (job.status === 'queued' || job.status === 'running') {
        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn btn-ghost btn-sm';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.onclick = async () => {
          await fetch('/api/downloads/cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: job.id }),
          });
          this.fetchDownloads();
        };
        card.appendChild(cancelBtn);
      }

      listEl.appendChild(card);
    }
  }

  // ---------------- Pair & Merge Chats ----------------
  openPairModal(video) {
    this.pairingVideo = video;
    document.getElementById('pair-video-name').textContent = video.name;
    const sel = document.getElementById('pair-chat-select');
    sel.innerHTML = '<option value="">-- Select chat file --</option>';
    for (const c of this.chats) {
      const opt = document.createElement('option');
      opt.value = c.rel_path;
      opt.textContent = `${c.name} (${formatBytes(c.size)})`;
      sel.appendChild(opt);
    }
    sel.value = video.chat?.rel_path || '';
    this.openModal('pair-modal');
  }

  async savePairModal(detach) {
    if (!this.pairingVideo) return;
    const chatRel = detach ? null : (document.getElementById('pair-chat-select').value || null);
    const res = await fetch('/api/pair-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ video_id: this.pairingVideo.id, chat_rel_path: chatRel }),
    });
    if (res.ok) {
      this.closeModal('pair-modal');
      await this.fetchLibrary(false);
      if (this.watch.isActive && this.watch.activeVideo?.id === this.pairingVideo.id) {
        const updated = this.videos.find((v) => v.id === this.pairingVideo.id);
        if (updated) await this.watch.refreshPairedChat(updated);
      }
      this.showToast(detach ? 'Chat detached' : 'Chat paired');
    }
  }

  openMergeModal() {
    const p1 = document.getElementById('merge-part1');
    const p2 = document.getElementById('merge-part2');
    const tv = document.getElementById('merge-target-video');
    document.getElementById('merge-status').classList.add('hidden');
    p1.innerHTML = '';
    p2.innerHTML = '';
    tv.innerHTML = '<option value="">Save as merged.chat.json</option>';

    for (const c of this.chats) {
      const label = `${c.name} (${formatBytes(c.size)})`;
      p1.add(new Option(label, c.rel_path));
      p2.add(new Option(label, c.rel_path));
    }
    for (const v of this.videos) {
      tv.add(new Option(v.title, v.id));
    }
    this.openModal('merge-modal');
  }

  async runMergeChats() {
    const statusEl = document.getElementById('merge-status');
    statusEl.classList.add('hidden');
    const part1 = document.getElementById('merge-part1').value;
    const part2 = document.getElementById('merge-part2').value;
    const offsetSec = parseFloat(document.getElementById('merge-offset').value || '0');
    const targetVideoId = document.getElementById('merge-target-video').value || null;

    const res = await fetch('/api/merge-chats', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        part1_rel_path: part1,
        part2_rel_path: part2,
        offset_sec: offsetSec,
        target_video_id: targetVideoId,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      statusEl.textContent = data.error || 'Merge failed';
      statusEl.classList.remove('hidden');
      return;
    }
    this.closeModal('merge-modal');
    await this.fetchLibrary(false);
    this.showToast(`Merged ${data.count.toLocaleString()} messages into ${data.rel_path}`);
  }

  // ---------------- Settings (.env) ----------------
  async openSettingsModal() {
    await this.fetchSettings();
    const env = this.settings?.env || {};
    document.getElementById('set-download-dir').value = env.DOWNLOAD_DIR || '';
    const qSel = document.getElementById('set-yt-quality');
    qSel.querySelectorAll('option[data-custom]').forEach((o) => o.remove());
    if (env.YT_QUALITY && !Array.from(qSel.options).some((o) => o.value === env.YT_QUALITY)) {
      const customOpt = new Option(`Custom (${env.YT_QUALITY})`, env.YT_QUALITY);
      customOpt.dataset.custom = '1';
      qSel.add(customOpt);
    }
    qSel.value = env.YT_QUALITY || 'bestvideo+bestaudio/best';
    document.getElementById('set-yt-format').value = env.YT_FORMAT || 'mkv';
    document.getElementById('set-yt-frags').value = env.YT_CONCURRENT_FRAGMENTS || '4';
    document.getElementById('set-kick-workers').value = env.KICK_CHAT_WORKERS || '1';
    document.getElementById('set-kick-delay').value = env.KICK_REQUEST_DELAY || '0.25';
    document.getElementById('set-thumb-seek').value = env.THUMBNAIL_SEEK_SEC || '120';
    document.getElementById('settings-status').classList.add('hidden');
    this.openModal('settings-modal');
  }

  async saveSettings() {
    const statusEl = document.getElementById('settings-status');
    statusEl.classList.add('hidden');
    const payload = {
      DOWNLOAD_DIR: document.getElementById('set-download-dir').value.trim(),
      YT_QUALITY: document.getElementById('set-yt-quality').value.trim(),
      YT_FORMAT: document.getElementById('set-yt-format').value,
      YT_CONCURRENT_FRAGMENTS: document.getElementById('set-yt-frags').value,
      KICK_CHAT_WORKERS: document.getElementById('set-kick-workers').value,
      KICK_REQUEST_DELAY: document.getElementById('set-kick-delay').value,
      THUMBNAIL_SEEK_SEC: document.getElementById('set-thumb-seek').value,
    };

    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ env: payload }),
    });
    const data = await res.json();
    if (!res.ok) {
      statusEl.textContent = data.error || 'Failed to save settings';
      statusEl.classList.remove('hidden');
      return;
    }
    this.settings = data;
    this.closeModal('settings-modal');
    this.fetchLibrary(false);
    this.showToast('Saved .env settings');
  }
}

window.app = new StreamVaultApp();
