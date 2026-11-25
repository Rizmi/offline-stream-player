/** Synchronized chat replay renderer with offline/circuit-broken image handling. */

const AVATAR_COLORS = [
  '#e53935', '#d81b60', '#8e24aa', '#5e35b1', '#3949ab',
  '#1e88e5', '#039be5', '#00acc1', '#00897b', '#43a047',
  '#7cb342', '#c0ca33', '#fb8c00', '#f4511e',
];
const MAX_VISIBLE_MESSAGES = 160;
const IMAGES_STORAGE_KEY = 'offline_player_external_images';
const MAX_CONSECUTIVE_IMG_FAILURES = 5;

export function formatTime(sec) {
  const total = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) {
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }
  return `${m}:${String(s).padStart(2, '0')}`;
}

function getAvatarColor(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

export class ChatController {
  constructor({ containerEl, countEl, pausedPillEl, imagesBtnEl, imagesLabelEl, onSeek }) {
    this.containerEl = containerEl;
    this.countEl = countEl;
    this.pausedPillEl = pausedPillEl;
    this.imagesBtnEl = imagesBtnEl;
    this.imagesLabelEl = imagesLabelEl;
    this.onSeek = onSeek;

    this.messages = [];
    this.currentIndex = 0;
    this.isUserScrolledUp = false;
    this.loadExternalImages = localStorage.getItem(IMAGES_STORAGE_KEY) === 'true';
    this.consecutiveFailures = 0;
    this.circuitBroken = false;
    this.currentTimeProvider = () => 0;

    this._bindEvents();
    this.updateImagesButton();
  }

  _bindEvents() {
    this.containerEl.addEventListener('scroll', () => {
      const distFromBottom = this.containerEl.scrollHeight - this.containerEl.scrollTop - this.containerEl.clientHeight;
      this.isUserScrolledUp = distFromBottom > 50;
      this.pausedPillEl.style.display = this.isUserScrolledUp ? 'inline-flex' : 'none';
    });

    this.pausedPillEl.addEventListener('click', () => {
      this.isUserScrolledUp = false;
      this.pausedPillEl.style.display = 'none';
      this.containerEl.scrollTop = this.containerEl.scrollHeight;
    });

    window.addEventListener('online', () => {
      this.consecutiveFailures = 0;
      this.circuitBroken = false;
      this.updateImagesButton();
    });
    window.addEventListener('offline', () => this.updateImagesButton());
  }

  shouldLoadImages() {
    return this.loadExternalImages && !this.circuitBroken && navigator.onLine;
  }

  updateImagesButton() {
    if (!this.loadExternalImages) {
      this.imagesLabelEl.textContent = 'Images: Off';
      this.imagesBtnEl.classList.remove('active');
    } else if (this.circuitBroken || !navigator.onLine) {
      this.imagesLabelEl.textContent = 'Images: Offline';
      this.imagesBtnEl.classList.remove('active');
    } else {
      this.imagesLabelEl.textContent = 'Images: On';
      this.imagesBtnEl.classList.add('active');
    }
  }

  toggleExternalImages() {
    this.loadExternalImages = !this.loadExternalImages;
    this.consecutiveFailures = 0;
    this.circuitBroken = false;
    localStorage.setItem(IMAGES_STORAGE_KEY, String(this.loadExternalImages));
    this.updateImagesButton();
    this.resync(this.currentTimeProvider());
  }

  clear() {
    this.messages = [];
    this.currentIndex = 0;
    this.countEl.textContent = '0 messages';
    this.containerEl.innerHTML = '<div class="chat-empty">No chat replay loaded for this stream.</div>';
  }

  async loadFromServer(chatId, currentSecProvider) {
    this.currentTimeProvider = currentSecProvider;
    this.countEl.textContent = 'Loading...';
    const res = await fetch(`/api/chat?id=${encodeURIComponent(chatId)}`);
    if (!res.ok) throw new Error('Failed to fetch chat replay');
    this.messages = await res.json();
    this.countEl.textContent = `${this.messages.length.toLocaleString()} messages`;
    this.resync(currentSecProvider());
  }

  async loadFromLocalFile(file, currentSecProvider) {
    this.currentTimeProvider = currentSecProvider;
    this.countEl.textContent = 'Parsing...';
    const text = await file.text();
    if (text.trim().startsWith('[')) {
      this.messages = JSON.parse(text);
    } else {
      const parsed = [];
      const lines = text.split('\n');
      for (let i = 0; i < lines.length; i++) {
        if (i > 0 && i % 3000 === 0) {
          await new Promise((r) => setTimeout(r, 0));
        }
        const line = lines[i].trim();
        if (!line) continue;
        try {
          const d = JSON.parse(line);
          const act = d.replayChatItemAction;
          if (!act) continue;
          const offset = parseInt(act.videoOffsetTimeMsec || '0', 10);
          if (offset < 0) continue;
          for (const a of act.actions || []) {
            const item = (a.addChatItemAction && a.addChatItemAction.item) || {};
            const msg = item.liveChatTextMessageRenderer || item.liveChatPaidMessageRenderer;
            if (!msg) continue;
            const author = (msg.authorName && msg.authorName.simpleText) || 'Anonymous';
            const pThumbs = (msg.authorPhoto && msg.authorPhoto.thumbnails) || [];
            let isMember = false;
            let badgeIcon = '';
            for (const b of msg.authorBadges || []) {
              const r = b.liveChatAuthorBadgeRenderer || {};
              if ((r.tooltip || '').toLowerCase().includes('member')) isMember = true;
              const bThumbs = (r.customThumbnail && r.customThumbnail.thumbnails) || [];
              if (bThumbs.length) badgeIcon = bThumbs[0].url;
            }
            const runs = [];
            for (const r of (msg.message && msg.message.runs) || []) {
              if (r.text) runs.push({ t: r.text });
              else if (r.emoji) {
                const em = r.emoji;
                const eThumbs = (em.image && em.image.thumbnails) || [];
                runs.push({
                  e: (em.shortcuts && em.shortcuts[0]) || em.emojiId || '',
                  u: eThumbs.length ? eThumbs[0].url : '',
                });
              }
            }
            parsed.push({
              o: offset,
              a: author,
              p: pThumbs.length ? pThumbs[0].url : '',
              m: isMember,
              bi: badgeIcon,
              r: runs,
            });
          }
        } catch (_) {}
      }
      this.messages = parsed;
    }
    this.messages.sort((a, b) => a.o - b.o);
    this.countEl.textContent = `${this.messages.length.toLocaleString()} messages`;
    this.resync(currentSecProvider());
  }

  _findIndex(targetMs) {
    let low = 0;
    let high = this.messages.length - 1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (this.messages[mid].o < targetMs) low = mid + 1;
      else high = mid - 1;
    }
    return low;
  }

  _createExternalImage({ src, className = '', title = '', alt = '', lazy = false, onFallback }) {
    const img = document.createElement('img');
    if (className) img.className = className;
    if (title) img.title = title;
    if (alt) img.alt = alt;
    if (lazy) img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.onload = () => {
      this.consecutiveFailures = 0;
    };
    img.onerror = () => {
      this.consecutiveFailures++;
      if (this.consecutiveFailures >= MAX_CONSECUTIVE_IMG_FAILURES && !this.circuitBroken) {
        this.circuitBroken = true;
        this.updateImagesButton();
      }
      onFallback(img);
    };
    img.src = src;
    return img;
  }

  _createMessageNode(msg) {
    const item = document.createElement('div');
    item.className = 'chat-item';
    const allowExternal = this.shouldLoadImages();

    const avatar = document.createElement('div');
    avatar.className = 'chat-avatar';
    const author = msg.a || 'Anonymous';
    const initial = (author.startsWith('@') ? author.slice(1, 2) : author.slice(0, 1)).toUpperCase() || '?';
    avatar.style.backgroundColor = getAvatarColor(author);

    if (allowExternal && msg.p) {
      avatar.appendChild(this._createExternalImage({
        src: msg.p,
        lazy: true,
        onFallback: (img) => {
          img.remove();
          avatar.textContent = initial;
        },
      }));
    } else {
      avatar.textContent = initial;
    }
    item.appendChild(avatar);

    const content = document.createElement('div');
    content.className = 'chat-content';

    const sec = Math.floor((msg.o || 0) / 1000);
    const timeSpan = document.createElement('span');
    timeSpan.className = 'chat-time';
    timeSpan.textContent = formatTime(sec);
    timeSpan.title = 'Click to seek video';
    timeSpan.onclick = () => this.onSeek(sec);
    content.appendChild(timeSpan);

    const authorSpan = document.createElement('span');
    authorSpan.className = 'chat-author' + (msg.m ? ' member' : '');
    authorSpan.textContent = author;

    if (allowExternal && msg.bi) {
      authorSpan.appendChild(this._createExternalImage({
        src: msg.bi,
        className: 'member-badge-icon',
        title: msg.b || 'Badge',
        onFallback: (img) => img.remove(),
      }));
    }
    content.appendChild(authorSpan);

    const textSpan = document.createElement('span');
    textSpan.className = 'chat-text';
    for (const run of msg.r || []) {
      if (run.t) {
        textSpan.appendChild(document.createTextNode(run.t));
      } else if (run.e) {
        if (allowExternal && run.u) {
          textSpan.appendChild(this._createExternalImage({
            src: run.u,
            className: 'chat-emoji',
            alt: run.e,
            title: run.e,
            onFallback: (img) => img.replaceWith(document.createTextNode(run.e)),
          }));
        } else {
          textSpan.appendChild(document.createTextNode(run.e));
        }
      }
    }
    content.appendChild(textSpan);
    item.appendChild(content);
    return item;
  }

  resync(currentSec) {
    if (!this.messages.length) return;
    const currentMs = (currentSec || 0) * 1000;
    this.currentIndex = this._findIndex(currentMs);
    this.containerEl.innerHTML = '';

    const start = Math.max(0, this.currentIndex - 35);
    const fragment = document.createDocumentFragment();
    for (let i = start; i < this.currentIndex; i++) {
      fragment.appendChild(this._createMessageNode(this.messages[i]));
    }
    this.containerEl.appendChild(fragment);
    if (!this.isUserScrolledUp) {
      this.containerEl.scrollTop = this.containerEl.scrollHeight;
    }
  }

  onTimeUpdate(currentSec) {
    if (!this.messages.length) return;
    const currentMs = currentSec * 1000;

    if (this.currentIndex > 0) {
      const refIdx = Math.min(this.currentIndex, this.messages.length - 1);
      if (Math.abs(currentMs - this.messages[refIdx].o) > 8000) {
        this.resync(currentSec);
        return;
      }
    }

    let added = false;
    const fragment = document.createDocumentFragment();
    while (this.currentIndex < this.messages.length && this.messages[this.currentIndex].o <= currentMs) {
      fragment.appendChild(this._createMessageNode(this.messages[this.currentIndex]));
      this.currentIndex++;
      added = true;
    }

    if (added) {
      this.containerEl.appendChild(fragment);
      while (this.containerEl.childElementCount > MAX_VISIBLE_MESSAGES) {
        this.containerEl.removeChild(this.containerEl.firstElementChild);
      }
      if (!this.isUserScrolledUp) {
        this.containerEl.scrollTop = this.containerEl.scrollHeight;
      }
    }
  }
}
