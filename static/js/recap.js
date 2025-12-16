// Global state
let ws = null;
const COPY_SHARE_TOOLTIP_DEFAULT = 'Copy Share URL';
const COPY_SHARE_TOOLTIP_SUCCESS = 'Copied Share URL';

// Initialize page
function init_page() {
    if (SHOW_EXISTING) {
        showExistingRecapScreen();
    } else if (IS_GENERATING) {
        connectToJobWebSocket(RECAP_ID);
    } else {
        // Completed recap - load and display
        loadCompletedRecap(RECAP_ID);
    }
}

// Show existing recap screen
function showExistingRecapScreen() {
    document.querySelector(".existing-recap").classList.remove("hidden");
}

// View existing recap (when user clicks button)
function viewExisting() {
    // Use pushState to navigate without refresh
    history.pushState({}, '', '/recap/' + RECAP_ID);
    document.querySelector(".existing-recap").classList.add("hidden");
    loadCompletedRecap(RECAP_ID);
}

// Generate new recap (replace existing)
function generateNew() {
    // Delete existing recap, then redirect to auth flow
    fetch('/api/recap/delete', {
        method: 'POST',
        credentials: 'same-origin'
    })
    .then(res => res.json())
    .then(() => {
        // Redirect to OAuth to start fresh
        window.location.href = "/auth/start";
    })
    .catch(err => {
        console.error("Failed to delete recap:", err);
        // Fallback: try to start auth anyway
        window.location.href = "/auth/start";
    });
}

// Connect to WebSocket for job updates
function connectToJobWebSocket(jobId) {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/ws/job/${jobId}`;

    showGeneratingScreen();

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log("WebSocket connected");
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleJobUpdate(data);
    };

    ws.onerror = (error) => {
        console.error("WebSocket error:", error);
    };

    ws.onclose = () => {
        console.log("WebSocket closed");
    };
}

// Handle job status updates
function handleJobUpdate(data) {
    const status = data.status;

    if (status === "queued") {
        updateStatus("You're in line to generate your recap. This might take a while.");
    } else if (status === "running") {
        updateStatus("We're generating your recap right now. This may take a few minutes.");
    } else if (status === "done") {
        // Job completed - show the recap
        showRecapSlides(data.slides);
    } else if (status === "error") {
        updateStatus("Something went wrong: " + (data.error || "Unknown error"));
    }
}

// Show generating screen
function showGeneratingScreen() {
    document.querySelector(".existing-recap").classList.add("hidden");
    document.querySelector(".actual-recap").classList.add("hidden");
    document.querySelector(".generating-recap").classList.remove("hidden");
}

// Update status text
function updateStatus(message) {
    const statusEl = document.getElementById("status");
    if (statusEl) {
        statusEl.textContent = message;
    }
}

// Load completed recap from server
function loadCompletedRecap(recapId) {
    fetch(`/api/recap/${recapId}`)
        .then(res => res.json())
        .then(data => {
            if (data.slides) {
                showRecapSlides(data.slides);
            }
        })
        .catch(err => {
            console.error("Failed to load recap:", err);
        });
}

// Renderer state
let slidesWrapper = null;
let slideElements = [];
let activeSlideIndex = 0;
let targetSlideIndex = 0;
let currentEffectInterval = null;
let recapData = null;
let activeEmojiIndex = null;
let emojiSwitchTimeout = null;
let emojiOverlay = null;
let emojiDrops = [];
let emojiAnimationFrame = null;
let lastEmojiFrame = null;
let activeEmojiEffect = null;
let fireworksParticles = [];
let fireworksCooldown = 0;
let starParticles = [];
let effectsEnabled = true;
let slideMeta = [];
let pickerCloseBound = false;
let promptInterval = null;
const normalizeShareImageUrl = (url) => {
    if (!url) return null;
    if (/^https?:\/\//i.test(url)) return url;
    return `${window.location.origin}${url}`;
};
let shareImageUrl = normalizeShareImageUrl(typeof SHARE_IMAGE_URL !== 'undefined' ? SHARE_IMAGE_URL : null);

function getFirstEmoji(str = '') {
    const chars = Array.from(str);
    const found = chars.find((c) => c.trim().length > 0);
    return found || '';
}

function getCurrentTheme() {
    const styles = getComputedStyle(document.body);
    const bg = styles.getPropertyValue('--bg')?.trim() || '#000';
    const fg = styles.getPropertyValue('--fg')?.trim() || '#fff';
    const ac = styles.getPropertyValue('--ac')?.trim() || fg;
    return { bg, fg, ac };
}

function loadEffectsPreference() {
    try {
        const stored = localStorage.getItem('recapEffectsEnabled');
        if (stored !== null) {
            effectsEnabled = stored === 'true';
        }
    } catch (err) {
        console.warn('Failed to read effects preference:', err);
    }

    const toggleEl = document.querySelector('.controls-icon');
    if (toggleEl) {
        toggleEl.classList.toggle('disabled', !effectsEnabled);
    }

    if (!effectsEnabled) {
        stopEmojiAnimation();
        if (emojiOverlay) {
            emojiOverlay.style.transition = 'none';
            emojiOverlay.style.opacity = '0';
        }
    }
}

function toggleEffects() {
    effectsEnabled = !effectsEnabled;
    try {
        localStorage.setItem('recapEffectsEnabled', String(effectsEnabled));
    } catch (err) {
        console.warn('Failed to store effects preference:', err);
    }
    const toggleEl = document.querySelector('.controls-icon');
    if (toggleEl) {
        toggleEl.classList.toggle('disabled', !effectsEnabled);
    }

    if (!effectsEnabled) {
        stopEmojiAnimation();
        if (emojiOverlay) {
            emojiOverlay.style.transition = 'none';
            emojiOverlay.style.opacity = '0';
        }
        activeEmojiIndex = null;
        return;
    }

    // Re-enable current slide's effect immediately
    switchEmojiEffect(activeSlideIndex, true);
}

function setupTitleSlide() {
    if (promptInterval) clearInterval(promptInterval);
}


function updatePickerLabel(index) {
    const picker = document.querySelector('.controls-picker');
    const list = document.querySelector('.controls-picker-list');
    if (!picker) return;
    const meta = slideMeta[index] || {};
    picker.textContent = meta.emoji || '☰';
    if (list) {
        list.querySelectorAll('.controls-picker-item').forEach((el) => {
            el.classList.toggle('active', parseInt(el.dataset.index, 10) === index);
        });
    }
}

function setControlsForIndex(index) {
    const controls = document.querySelector('.controls');
    if (!controls) return;
    if (index === 0) {
        controls.classList.add('title-hidden');
        controls.classList.remove('title-visible');
    } else {
        controls.classList.remove('title-hidden');
        controls.classList.add('title-visible');
    }
}

function startTitleHints() {
    const hintsEl = slideElements[0]?.querySelector('.title-hints');
    if (!hintsEl) return;
    if (promptInterval) clearInterval(promptInterval);
    const isMobile = window.matchMedia('(pointer: coarse)').matches;
    const prompts = isMobile
        ? 'Tap anywhere to begin'
        : 'Click anywhere to begin';
    let idx = 0;
    const render = () => {
        hintsEl.innerHTML = '';
        const span = document.createElement('span');
        span.textContent = prompts;
        hintsEl.appendChild(span);
    };
    render();
}

function stopTitleHints() {
    if (promptInterval) clearInterval(promptInterval);
    promptInterval = null;
}

function populatePickerOptions(listEl) {
    if (!listEl) return;
    listEl.innerHTML = '';
    slideMeta.forEach((meta, idx) => {
        const item = document.createElement('div');
        item.className = 'controls-picker-item';
        item.dataset.index = idx;
        item.textContent = `${meta.emoji ? meta.emoji + ' ' : ''}${meta.title}`;
        listEl.appendChild(item);
    });
}

function buildSlidePicker() {
    const picker = document.querySelector('.controls-picker');
    const list = document.querySelector('.controls-picker-list');
    if (!picker || !list) return;

    picker.textContent = slideMeta[activeSlideIndex]?.emoji || '☰';

    picker.onclick = (e) => {
        e.stopPropagation();
        if (list.classList.contains('open')) {
            list.classList.remove('open');
        } else {
            populatePickerOptions(list);
            list.classList.add('open');
            updatePickerLabel(activeSlideIndex);
        }
    };

    if (!pickerCloseBound) {
        document.addEventListener('click', (e) => {
            if (!picker.contains(e.target) && !list.contains(e.target)) {
                list.classList.remove('open');
            }
        });
        list.addEventListener('click', (e) => {
            const target = e.target.closest('.controls-picker-item');
            if (!target) return;
            const idx = parseInt(target.dataset.index, 10);
            if (Number.isFinite(idx)) {
                scrollToSlide(idx);
            }
            list.classList.remove('open');
        });
        pickerCloseBound = true;
    }
}

function applyDynamicScaling() {
    if (!slideElements.length) return;

    slideElements.forEach((slide) => {
        const content = slide.querySelector('.slide-content');
        if (!content) return;

        // Reset scale before measuring natural size
        slide.style.setProperty('--content-scale', '1');

        const availableHeight = Math.max(0, slide.clientHeight - 32);
        const availableWidth = Math.max(0, slide.clientWidth - 32);
        const contentHeight = content.scrollHeight;
        const contentWidth = content.scrollWidth;

        const isListSlide = !!slide.querySelector('.list-container');

        let scale = 1;
        if (contentHeight > 0 && contentWidth > 0 && availableHeight > 0 && availableWidth > 0) {
            const heightScale = availableHeight / contentHeight;
            const widthScale = availableWidth / contentWidth;
            // For list slides, keep text legible and rely on scrolling—only scale to fit width.
            scale = isListSlide ? Math.min(1, widthScale) : Math.min(1, heightScale, widthScale);
        }

        // Keep a sensible floor to avoid microscopic text on any slide type.
        const minScale = isListSlide ? 0.9 : 0.6;
        slide.style.setProperty('--content-scale', Math.max(scale, minScale).toFixed(3));
    });
}

// Color conversion utilities
function hexToRGB(hex) {
    const short = /^#?([a-f\d])([a-f\d])([a-f\d])$/i.exec(hex);
    if (short) {
        return {
            r: parseInt(short[1] + short[1], 16),
            g: parseInt(short[2] + short[2], 16),
            b: parseInt(short[3] + short[3], 16)
        };
    }

    const full = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
    return full ? {
        r: parseInt(full[1], 16),
        g: parseInt(full[2], 16),
        b: parseInt(full[3], 16)
    } : { r: 0, g: 0, b: 0 };
}

function rgbToHSV(r, g, b) {
    r /= 255;
    g /= 255;
    b /= 255;

    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const d = max - min;

    let h = 0;
    const s = max === 0 ? 0 : d / max;
    const v = max;

    if (max !== min) {
        switch (max) {
            case r: h = ((g - b) / d + (g < b ? 6 : 0)) / 6; break;
            case g: h = ((b - r) / d + 2) / 6; break;
            case b: h = ((r - g) / d + 4) / 6; break;
        }
    }

    return { h: h * 360, s: s * 100, v: v * 100 };
}

function hsvToRGB(h, s, v) {
    h /= 360;
    s /= 100;
    v /= 100;

    const i = Math.floor(h * 6);
    const f = h * 6 - i;
    const p = v * (1 - s);
    const q = v * (1 - f * s);
    const t = v * (1 - (1 - f) * s);

    let r, g, b;
    switch (i % 6) {
        case 0: r = v; g = t; b = p; break;
        case 1: r = q; g = v; b = p; break;
        case 2: r = p; g = v; b = t; break;
        case 3: r = p; g = q; b = v; break;
        case 4: r = t; g = p; b = v; break;
        case 5: r = v; g = p; b = q; break;
    }

    return {
        r: Math.round(r * 255),
        g: Math.round(g * 255),
        b: Math.round(b * 255)
    };
}

function hexToHSV(hex) {
    const rgb = hexToRGB(hex);
    return rgbToHSV(rgb.r, rgb.g, rgb.b);
}

function hsvToHex(hsv) {
    const rgb = hsvToRGB(hsv.h, hsv.s, hsv.v);
    return `#${((1 << 24) + (rgb.r << 16) + (rgb.g << 8) + rgb.b).toString(16).slice(1)}`;
}

function blendColorsHSV(hex1, hex2, ratio) {
    const hsv1 = hexToHSV(hex1);
    const hsv2 = hexToHSV(hex2);

    // Move hue along the shortest arc to avoid long wraps (e.g., 350° -> 10° goes +20°, not -340°)
    let hueDelta = hsv2.h - hsv1.h;
    if (hueDelta > 180) hueDelta -= 360;
    if (hueDelta < -180) hueDelta += 360;

    const blended = {
        h: (hsv1.h + hueDelta * ratio + 360) % 360,
        s: hsv1.s + (hsv2.s - hsv1.s) * ratio,
        v: hsv1.v + (hsv2.v - hsv1.v) * ratio
    };

    return hsvToHex(blended);
}

// Variable substitution
function substituteVariables(text, data) {
    if (typeof text !== 'string') return text;
    return text.replace(/%\{([^}]+)\}/g, (match, varName) => {
        return data[varName] !== undefined ? data[varName] : match;
    });
}

// Effect animations
function runCountUpEffect(element, effect, data) {
    const parseNumberParts = (raw) => {
        const text = substituteVariables(String(raw ?? 0), data);
        const match = text.match(/^([^0-9.-]*)(-?\d*\.?\d+)(.*)$/);
        if (match) {
            const [, prefix, numStr, suffix] = match;
            const decimals = (numStr.split('.')[1] || '').length;
            return {
                value: parseFloat(numStr) || 0,
                prefix: prefix || '',
                suffix: suffix || '',
                decimals
            };
        }
        return {
            value: parseFloat(text) || 0,
            prefix: '',
            suffix: '',
            decimals: 0
        };
    };

    const stepPrecision = (val) => {
        if (val === undefined || val === null) return 0;
        const str = String(val);
        const idx = str.indexOf('.');
        return idx >= 0 ? str.length - idx - 1 : 0;
    };

    const startParts = parseNumberParts(effect.start_num);
    const endParts = parseNumberParts(effect.end_num);
    const precision = Math.max(startParts.decimals, endParts.decimals, stepPrecision(effect.step));
    const prefix = effect.prefix ?? startParts.prefix ?? endParts.prefix ?? '';
    const suffix = effect.suffix ?? endParts.suffix ?? startParts.suffix ?? '';

    const startNum = startParts.value;
    const endNum = endParts.value;
    const duration = 1500;
    const startTime = Date.now();

    function animate() {
        const elapsed = Date.now() - startTime;
        const progress = Math.min(elapsed / duration, 1);
        // Ease-out curve: starts fast, slows down at the end
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = startNum + (endNum - startNum) * eased;
        const formatted = precision > 0 ? current.toFixed(precision) : Math.round(current).toString();
        element.textContent = `${prefix}${formatted}${suffix}`;

        if (progress < 1) {
            requestAnimationFrame(animate);
        }
    }

    animate();
}

function runTextRevealEffect(element, effect, data) {
    const scrollOptions = effect.reveal_scroll_options || [];
    const totalDuration = effect.scroll_duration || 1000;
    const finalText = substituteVariables(String(effect.reveal_text || ''), data);

    if (currentEffectInterval) {
        clearInterval(currentEffectInterval);
    }

    if (scrollOptions.length === 0) {
        element.textContent = finalText;
        return;
    }

    const startTime = Date.now();
    let lastIndex = -1;

    function animate() {
        const elapsed = Date.now() - startTime;
        const progress = Math.min(elapsed / totalDuration, 1);
        // Ease-out curve: starts fast, slows down at the end
        const eased = 1 - Math.pow(1 - progress, 3);
        const currentIndex = Math.floor(eased * scrollOptions.length);

        if (currentIndex !== lastIndex && currentIndex < scrollOptions.length) {
            element.textContent = scrollOptions[currentIndex];
            lastIndex = currentIndex;
        }

        if (progress < 1) {
            requestAnimationFrame(animate);
        } else {
            element.textContent = finalText;
        }
    }

    animate();
}

function runCryptoRevealEffect(element, effect, data) {
    if (!element.classList.contains('list-container')) return;

    const items = element.querySelectorAll('.list-item');
    const itemRevealDuration = effect.item_duration || 800;  // ms per item
    const staggerDelay = effect.stagger_delay || 150;  // delay between items
    const cryptoChars = effect.crypto_chars || '▓▒░█▄▀▌│┤┐└┴┬├─┼╔╗╚╝║═╬';

    // Shared state for synchronized shuffling across all items
    const sharedState = {
        shuffleCounter: 0,  // Increments when any element reveals a character
        triggerShuffle: function() {
            this.shuffleCounter++;
        }
    };

    // Start ALL animations immediately so they all watch the shuffle counter
    items.forEach((item, index) => {
        const titleEl = item.querySelector('.list-item-title');
        const subtitleEl = item.querySelector('.list-item-subtitle');
        const titleText = item.dataset.titleText || '';
        const subtitleText = item.dataset.subtitleText || '';

        // Calculate stagger delay for this item
        const titleDelay = index * staggerDelay;
        const subtitleDelay = titleDelay + (itemRevealDuration * 0.3);

        // Start animations immediately with stagger delay built in
        animateCryptoReveal(titleEl, titleText, itemRevealDuration, cryptoChars, sharedState, titleDelay);
        animateCryptoReveal(subtitleEl, subtitleText, itemRevealDuration * 0.7, cryptoChars, sharedState, subtitleDelay);
    });
}

function animateCryptoReveal(element, finalText, duration, cryptoChars, sharedState, startDelay = 0) {
    if (!finalText) {
        element.textContent = '';
        return;
    }

    const chars = Array.from(cryptoChars);
    const finalChars = Array.from(finalText);
    const textLength = finalChars.length;
    const globalStartTime = Date.now();
    let lastRevealCount = 0;
    let lastShuffleCounter = sharedState ? sharedState.shuffleCounter : 0;
    const randomCharFor = (targetChar) => {
        // Preserve punctuation/whitespace/structure characters; only swap alphanumerics
        return /[A-Za-z0-9]/.test(targetChar)
            ? chars[Math.floor(Math.random() * chars.length)]
            : targetChar;
    };

    let randomChars = finalChars.map((ch) => randomCharFor(ch));

    function animate() {
        const elapsed = Date.now() - globalStartTime;

        // Calculate progress accounting for start delay
        let progress = 0;
        if (elapsed > startDelay) {
            progress = Math.min((elapsed - startDelay) / duration, 1);
        }

        // Ease-out curve (consistent with other effects)
        const eased = 1 - Math.pow(1 - progress, 3);

        // Number of characters to reveal from left
        const revealCount = Math.floor(eased * textLength);

        // Trigger global shuffle when this element reveals a new character
        if (revealCount > lastRevealCount) {
            lastRevealCount = revealCount;
            if (sharedState) {
                sharedState.triggerShuffle();
            }
        }

        // Reshuffle random characters when ANY element reveals a character
        if (sharedState && sharedState.shuffleCounter > lastShuffleCounter) {
            lastShuffleCounter = sharedState.shuffleCounter;
            // Generate new random characters for unrevealed positions
            randomChars = finalChars.map((ch) => randomCharFor(ch));
        }

        let displayText = '';

        for (let i = 0; i < textLength; i++) {
            if (i < revealCount) {
                // Revealed: show actual character
                displayText += finalChars[i];
            } else {
                // Not revealed: show random crypto character
                displayText += randomChars[i];
            }
        }

        element.textContent = displayText;

        // Debug: Log character count
        console.log(`[${element.className}] Expected: ${textLength}, Actual: ${displayText.length}, Progress: ${progress.toFixed(2)}`);

        // Continue animating until this element is done (factoring in delay)
        if (elapsed < startDelay + duration) {
            requestAnimationFrame(animate);
        } else {
            // Final state: show actual text
            element.textContent = finalText;
            console.log(`[${element.className}] FINAL - Expected: ${textLength}, Actual: ${finalText.length}`);
        }
    }

    animate();
}

function stopEmojiAnimation(clearNodes = true) {
    if (emojiAnimationFrame) {
        cancelAnimationFrame(emojiAnimationFrame);
    }
    emojiAnimationFrame = null;
    emojiDrops = [];
    lastEmojiFrame = null;
    fireworksParticles = [];
    fireworksCooldown = 0;
    activeEmojiEffect = null;
    starParticles = [];
    if (clearNodes && emojiOverlay) {
        emojiOverlay.innerHTML = '';
    }
}

function animateEmojiRain(timestamp) {
    if (activeEmojiEffect !== 'rain') return;
    if (!emojiOverlay || emojiDrops.length === 0) return;

    if (!lastEmojiFrame) {
        lastEmojiFrame = timestamp;
    }

    const dt = Math.min((timestamp - lastEmojiFrame) / 1000, 0.05);
    lastEmojiFrame = timestamp;
    const height = window.innerHeight;
    const width = window.innerWidth;

    emojiDrops.forEach((drop) => {
        drop.y += drop.speed * dt;
        drop.rot += drop.rotSpeed * dt;

        if (drop.y > height + 200) {
            drop.y = -160 - Math.random() * height * 0.6;
            drop.x = Math.random() * width;
            drop.speed = 140 + Math.random() * 220;
            drop.rot = Math.random() * 360;
            drop.rotSpeed = -60 + Math.random() * 140;
        }

        drop.el.style.transform = `translate3d(${drop.x}px, ${drop.y}px, 0) rotate(${drop.rot}deg) scale(${drop.scale})`;
    });

    emojiAnimationFrame = requestAnimationFrame(animateEmojiRain);
}

function startEmojiRain(emjString, immediate = false) {
    if (!emojiOverlay) return;

    stopEmojiAnimation();
    activeEmojiEffect = 'rain';

    const chars = Array.from(emjString || '').filter((c) => c.trim().length > 0);
    if (chars.length === 0) {
        emojiOverlay.style.opacity = '0';
        return;
    }

    // Reset visual state before seeding drops
    emojiOverlay.style.transition = 'none';
    emojiOverlay.style.opacity = '0';

    const dropCount = Math.max(30, Math.min(70, Math.floor(window.innerWidth / 18)));
    const height = window.innerHeight;
    const width = window.innerWidth;

    for (let i = 0; i < dropCount; i++) {
        const ch = chars[Math.floor(Math.random() * chars.length)];
        const el = document.createElement('span');
        el.className = 'emj-drop';
        el.textContent = ch;

        const scale = 0.55 + Math.random() * 1.1;
        const speed = 160 + Math.random() * 240;
        const rot = Math.random() * 360;
        const rotSpeed = -70 + Math.random() * 140;
        const x = Math.random() * width;
        const y = Math.random() < 0.55 ? Math.random() * height : -Math.random() * height * 0.6;

        el.style.fontSize = `${36 + Math.random() * 20}px`;
        el.style.opacity = '0.9';

        el.style.transform = `translate3d(${x}px, ${y}px, 0) rotate(${rot}deg) scale(${scale})`;

        emojiOverlay.appendChild(el);
        emojiDrops.push({ el, x, y, speed, rot, rotSpeed, scale });
    }

    // Prime one frame so drops are positioned before fade in
    lastEmojiFrame = performance.now();
    animateEmojiRain(lastEmojiFrame);

    if (immediate) {
        emojiOverlay.style.transition = 'none';
        emojiOverlay.style.opacity = '1';
    } else {
        requestAnimationFrame(() => {
            setTimeout(() => {
                emojiOverlay.style.transition = 'opacity 0.5s ease';
                emojiOverlay.style.opacity = '1';
            }, 250);
        });
    }

    lastEmojiFrame = null;
}

function createFireworkBurst(chars, width, height) {
    const count = 10 + Math.floor(Math.random() * 8);
    const cx = width * (0.2 + Math.random() * 0.6);
    const cy = height * (0.25 + Math.random() * 0.45);
    const particles = [];

    for (let i = 0; i < count; i++) {
        const ch = chars[Math.floor(Math.random() * chars.length)];
        const el = document.createElement('span');
        el.className = 'emj-drop';
        el.textContent = ch;

        const angle = Math.random() * Math.PI * 2;
        const speed = 200 + Math.random() * 200;
        const vx = Math.cos(angle) * speed;
        const vy = Math.sin(angle) * speed;
        const scale = 0.8 + Math.random() * 0.9;
        const rot = Math.random() * 360;
        const rotSpeed = -140 + Math.random() * 280;
        const duration = 1.0 + Math.random() * 0.6;

        el.style.fontSize = `${40 + Math.random() * 22}px`;
        el.style.opacity = '1';
        el.style.transform = `translate3d(${cx}px, ${cy}px, 0) rotate(${rot}deg) scale(${scale})`;

        emojiOverlay.appendChild(el);
        particles.push({ el, x: cx, y: cy, vx, vy, rot, rotSpeed, scale, life: 0, duration });
    }

    // 30% chance of a second burst
    if (Math.random() < 0.3) {
        const burst = createFireworkBurst(chars, width, height);
        fireworksParticles.push(...burst);
    }

    return particles;
}

function animateEmojiFireworks(timestamp) {
    if (activeEmojiEffect !== 'fireworks') return;
    if (!emojiOverlay) return;

    if (!lastEmojiFrame) {
        lastEmojiFrame = timestamp;
    }

    const dt = Math.min((timestamp - lastEmojiFrame) / 1000, 0.05);
    lastEmojiFrame = timestamp;
    const height = window.innerHeight;
    const gravity = 320;

    fireworksCooldown -= dt * 1000;

    fireworksParticles.forEach((p) => {
        p.vy += gravity * dt;
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        p.rot += p.rotSpeed * dt;
        p.life += dt;

        const progress = Math.min(p.life / p.duration, 1);
        const fade = 1 - progress;
        p.el.style.opacity = fade;
        const scale = p.scale * (1 - 0.15 * progress);
        p.el.style.transform = `translate3d(${p.x}px, ${p.y}px, 0) rotate(${p.rot}deg) scale(${scale})`;
    });

    fireworksParticles = fireworksParticles.filter((p) => {
        if (p.life >= p.duration || p.y > height + 200) {
            p.el.remove();
            return false;
        }
        return true;
    });

    if (fireworksParticles.length < 28 && fireworksCooldown <= 0) {
        const chars = emojiOverlay.dataset.emojis;
        if (chars && chars.length) {
            const burst = createFireworkBurst(chars, window.innerWidth, window.innerHeight);
            fireworksParticles.push(...burst);
            fireworksCooldown = 280 + Math.random() * 420;
        }
    }

    emojiAnimationFrame = requestAnimationFrame(animateEmojiFireworks);
}

function animateStarfield(timestamp) {
    if (activeEmojiEffect !== 'stars') return;
    if (!emojiOverlay || starParticles.length === 0) return;

    if (!lastEmojiFrame) {
        lastEmojiFrame = timestamp;
    }

    const dt = Math.min((timestamp - lastEmojiFrame) / 1000, 0.05);
    lastEmojiFrame = timestamp;
    const height = window.innerHeight;
    const width = window.innerWidth;

    starParticles.forEach((s) => {
        s.y += s.speed * dt;
        if (s.y > height + 10) {
            s.y = -10 - Math.random() * 40;
            s.x = Math.random() * width;
        }

        s.phase += s.twinkle * dt;
        const twinkle = 0.6 + 0.4 * Math.sin(s.phase);
        s.el.style.opacity = (s.baseOpacity * twinkle).toFixed(2);
        s.el.style.transform = `translate3d(${s.x}px, ${s.y}px, 0) scale(${s.scale})`;
    });

    emojiAnimationFrame = requestAnimationFrame(animateStarfield);
}

function startStarfield(immediate = false) {
    if (!emojiOverlay) return;

    stopEmojiAnimation();
    activeEmojiEffect = 'stars';
    emojiOverlay.dataset.emojis = '';

    const count = Math.max(80, Math.min(160, Math.floor(window.innerWidth / 6)));
    const height = window.innerHeight;
    const width = window.innerWidth;

    emojiOverlay.style.transition = 'none';
    emojiOverlay.style.opacity = '0';

    for (let i = 0; i < count; i++) {
        const el = document.createElement('span');
        el.className = 'star-dot';

        const scale = 0.5 + Math.random() * 1.6;
        const x = Math.random() * width;
        const y = Math.random() * height;
        const speed = 4 + Math.random() * 22;
        const twinkle = 2 + Math.random() * 3.5;
        const baseOpacity = 0.35 + Math.random() * 0.6;

        el.style.opacity = baseOpacity.toFixed(2);
        el.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${scale})`;

        emojiOverlay.appendChild(el);
        starParticles.push({ el, x, y, speed, twinkle, phase: Math.random() * Math.PI * 2, baseOpacity, scale });
    }

    // Prime frame before fade in
    lastEmojiFrame = performance.now();
    animateStarfield(lastEmojiFrame);

    if (immediate) {
        emojiOverlay.style.transition = 'none';
        emojiOverlay.style.opacity = '1';
    } else {
        requestAnimationFrame(() => {
            emojiOverlay.style.transition = 'opacity 0.25s ease';
            emojiOverlay.style.opacity = '1';
        });
    }

    lastEmojiFrame = null;
    emojiAnimationFrame = requestAnimationFrame(animateStarfield);
}

function startEmojiFireworks(emjString, immediate = false) {
    if (!emojiOverlay) return;

    stopEmojiAnimation();
    activeEmojiEffect = 'fireworks';

    const chars = Array.from(emjString || '').filter((c) => c.trim().length > 0);
    if (chars.length === 0) {
        emojiOverlay.style.opacity = '0';
        return;
    }

    emojiOverlay.dataset.emojis = chars.join('');
    emojiOverlay.style.transition = 'none';
    emojiOverlay.style.opacity = '0';

    // Seed a couple bursts immediately
    const burstCount = 3;
    for (let i = 0; i < burstCount; i++) {
        fireworksParticles.push(...createFireworkBurst(chars, window.innerWidth, window.innerHeight));
    }
    fireworksCooldown = 300 + Math.random() * 350;

    // Prime positions before fade in
    lastEmojiFrame = performance.now();
    animateEmojiFireworks(lastEmojiFrame);

    if (immediate) {
        emojiOverlay.style.transition = 'none';
        emojiOverlay.style.opacity = '1';
    } else {
        requestAnimationFrame(() => {
            emojiOverlay.style.transition = 'opacity 0.25s ease';
            emojiOverlay.style.opacity = '1';
        });
    }

    lastEmojiFrame = null;
}

function fadeOutEmojiOverlay() {
    if (!emojiOverlay) return;
    emojiOverlay.style.transition = 'opacity 0.5s ease';
    emojiOverlay.style.opacity = '0';
    emojiOverlay.dataset.emojis = '';
}

// Build slide DOM elements
function buildSlideElements(cards, data) {
    recapData = data;
    slideMeta = [];
    const container = document.getElementById('recap-container');
    container.innerHTML = '';

    const overlay = document.createElement('div');
    overlay.className = 'emj-overlay';
    container.appendChild(overlay);
    emojiOverlay = overlay;

    const wrapper = document.createElement('div');
    wrapper.className = 'slides-wrapper';
    slideElements = [];

    const userName = (data.user_name || '').trim() || 'Your';

    // Check for embed mode
    const isEmbed = new URLSearchParams(window.location.search).get('embed') === 'true';

    // Customize title slide for embed mode
    const cardsWithTitle = [
        {
            title: 'Your Schoology Recap',
            desc: isEmbed
                ? `${userName}'s stats from their Schoology data`
                : 'Explore your Schoology data stats and share with friends with the buttons at the bottom.',
            bg: '#000',
            fg: '#000',
            ac: '#000',
            emj: '👋',
            type: 'static',
            effect: {}
        },
        ...cards
    ];

    cardsWithTitle.forEach((card, index) => {
        const isTitleSlide = index === 0;
        const slide = document.createElement('div');
        slide.className = 'slide';
        slide.dataset.index = index;
        slide.dataset.bg = card.bg || '#2bc24e';
        slide.dataset.fg = card.fg || '#fff';
        slide.dataset.ac = card.ac || '#ebce2a';
        slide.dataset.emj = card.emj || '';
        slide.dataset.firstEmj = getFirstEmoji(card.emj || '');
        if (card.effect && card.effect['emj-effect']) {
            slide.dataset.emjEffect = card.effect['emj-effect'];
        }

        if (isTitleSlide) {
            slide.classList.add('title-slide');
        }

        const slideContent = document.createElement('div');
        slideContent.className = 'slide-content';

        const rawTitle = substituteVariables(card.title, data);
        const metaTitle = isTitleSlide ? 'Recap' : rawTitle;
        slideMeta.push({
            title: metaTitle,
            emoji: getFirstEmoji(card.emj || '')
        });

        const title = document.createElement('p');
        title.className = 'slide-title';
        if (isTitleSlide) {
            title.innerHTML = 'Tech Club Presents:';
            // bigger than normal
            title.style.fontSize = 'clamp(36px, 5vw, 48px)';
        } else {
            title.textContent = rawTitle;
        }

        const big = document.createElement('h1');
        big.className = 'slide-big';
        big.dataset.type = card.type || 'static';
        if (card.effect) {
            big.dataset.effect = JSON.stringify(card.effect);
        }

        if (isTitleSlide) {
            big.textContent = userName ? `${userName}'s Recap` : 'Your Schoology Recap';
            // smaller than usual
            big.style.fontSize = 'clamp(40px, 6vw, 60px)';
            big.style.fontWeight = '600';
        } else if (card.type === 'num_countup' || card.type === 'countup') {
            const startNum = substituteVariables(String(card.effect?.start_num || 0), data);
            big.textContent = startNum;
        } else if (card.type === 'text_reveal') {
            const firstOption = card.effect?.reveal_scroll_options?.[0] || '';
            big.textContent = firstOption;
        } else if (card.type === 'list') {
            // Create container for list items
            big.innerHTML = '';
            big.classList.add('list-container');

            // Get list template from effect
            const listTemplate = card.effect?.list?.[0];
            if (!listTemplate) {
                big.textContent = 'No data';
            } else {
                // Get actual data array (e.g., top_classmates)
                const dataKey = card.effect?.data_key || 'top_classmates';
                const listData = data[dataKey] || [];

                // Create DOM elements for each list item
                listData.forEach((item, idx) => {
                    const itemEl = document.createElement('div');
                    itemEl.className = 'list-item';
                    itemEl.dataset.itemIndex = idx;

                    // Handle array fields (like sections) - join them
                    const itemForSubstitution = {...item};
                    if (Array.isArray(itemForSubstitution.sections)) {
                        itemForSubstitution.sections = itemForSubstitution.sections.join(', ');
                    }

                    // Substitute variables for this specific item
                    const titleText = substituteVariables(listTemplate.title, itemForSubstitution);
                    const subtitleText = substituteVariables(listTemplate.subtitle, itemForSubstitution);

                    // Store final text in data attributes
                    itemEl.dataset.titleText = titleText;
                    itemEl.dataset.subtitleText = subtitleText;

                    // Create title and subtitle elements (initially encrypted with random chars)
                    const cryptoChars = card.effect?.crypto_chars || 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:#.!?';
                    const randomCharFor = (targetChar) =>
                        /[A-Za-z0-9]/.test(targetChar)
                            ? cryptoChars[Math.floor(Math.random() * cryptoChars.length)]
                            : targetChar;

                    const titleDiv = document.createElement('div');
                    titleDiv.className = 'list-item-title';
                    // Preserve punctuation/spacing structure in the encrypted view
                    const randomTitleText = Array.from(titleText).map((ch) => randomCharFor(ch)).join('');
                    titleDiv.textContent = randomTitleText;

                    const subtitleDiv = document.createElement('div');
                    subtitleDiv.className = 'list-item-subtitle';
                    // Preserve punctuation/spacing structure in the encrypted view
                    const randomSubtitleText = Array.from(subtitleText).map((ch) => randomCharFor(ch)).join('');
                    subtitleDiv.textContent = randomSubtitleText;

                    itemEl.appendChild(titleDiv);
                    itemEl.appendChild(subtitleDiv);
                    big.appendChild(itemEl);
                });
            }
        } else if (card.type === 'grid') {
            // Create grid summary of all previous cards
            big.innerHTML = '';
            big.classList.add('slide-grid-container');

            // Pull colors from original cards (excluding the title slide)
            const originalCards = cards.slice(0, 7); // First 7 cards (not including the grid card)

            // Helper function to create a grid item with position
            const createGridItem = (title, value, sourceCard, col, row, isCenter = false, isDouble = false) => {
                const gridItem = document.createElement('div');
                gridItem.className = 'grid-item';
                if (isCenter) {
                    gridItem.classList.add('center');
                }
                if (isDouble) {
                    gridItem.classList.add('double');
                }
                gridItem.style.backgroundColor = sourceCard.bg;
                gridItem.style.color = sourceCard.fg;
                gridItem.dataset.col = col;
                gridItem.dataset.row = row;
                gridItem.dataset.isDouble = isDouble ? 'true' : 'false';

                const itemTitle = document.createElement('div');
                itemTitle.className = 'grid-item-title';
                itemTitle.textContent = title;

                const itemValue = document.createElement('div');
                itemValue.className = 'grid-item-value';
                itemValue.textContent = value;

                gridItem.appendChild(itemTitle);
                gridItem.appendChild(itemValue);
                return gridItem;
            };

            // Create grid items with positions (col, row)
            // Column 1 (col 0): Cards 0, 1, 2
            big.appendChild(createGridItem(originalCards[0].title, data.total_assignments, originalCards[0], 0, 0));
            big.appendChild(createGridItem(originalCards[1].title, data.busiest_month, originalCards[1], 0, 1));
            big.appendChild(createGridItem(originalCards[2].title, data.weekend_subs, originalCards[2], 0, 2));

            // Column 2 (col 1): Cards 3, CENTER, 4
            big.appendChild(createGridItem(originalCards[3].title, data.weekday_subs, originalCards[3], 1, 0));
            big.appendChild(createGridItem('Your Recap', `${data.user_name || 'Your'} 2025`, card, 1, 1, true));
            big.appendChild(createGridItem(originalCards[4].title, data.avg_procrastination + ' hrs', originalCards[4], 1, 2));

            // Column 3 (col 2): Card 5, Top classmates (double height at row 1-2)
            big.appendChild(createGridItem(originalCards[5].title, data.night_owl_subs, originalCards[5], 2, 0));
            const topClassmatesValue = (data.top_classmates || []).slice(0, 3).map(c => c.name).join(', ') || 'None';
            big.appendChild(createGridItem(originalCards[6].title, topClassmatesValue, originalCards[6], 2, 1, false, true));

            // Calculate and set positions
            const positionGridItems = () => {
                const container = big;
                const containerWidth = container.offsetWidth;
                const gap = 20; // Match CSS gap
                const squareSize = ((containerWidth*0.5) - (gap * 2)) / 3;

                container.querySelectorAll('.grid-item').forEach(item => {
                    const col = parseInt(item.dataset.col);
                    const row = parseInt(item.dataset.row);
                    const isDouble = item.dataset.isDouble === 'true';

                    const left = col * (squareSize + gap);
                    const top = row * (squareSize + gap);
                    const width = squareSize;
                    const height = isDouble ? (squareSize * 2 + gap) : squareSize;

                    item.style.left = `${left}px`;
                    item.style.top = `${top}px`;
                    item.style.width = `${width}px`;
                    item.style.height = `${height}px`;
                });
            };

            // Position items after a brief delay to ensure container is sized
            setTimeout(positionGridItems, 10);
            window.addEventListener('resize', positionGridItems);
        } else {
            big.textContent = substituteVariables(card.title, data);
        }

        const desc = document.createElement('p');
        desc.className = 'slide-desc';
        desc.textContent = substituteVariables(card.desc, data);

        slideContent.appendChild(title);
        slideContent.appendChild(big);
        slideContent.appendChild(desc);

        if (isTitleSlide) {
            const hints = document.createElement('div');
            hints.className = 'title-hints';
            slideContent.appendChild(hints);
        }

        slide.appendChild(slideContent);
        wrapper.appendChild(slide);
        slideElements.push(slide);
    });

    const name = (data.user_name || '').trim();
    const userNameEl = document.getElementById('user-name');
    if (userNameEl) {
        userNameEl.textContent = name;
    }

    container.appendChild(wrapper);
    slidesWrapper = wrapper;

    loadEffectsPreference();

    // populate emoji icon
    const picker = document.querySelector('.controls-picker');
    if (picker) {
        picker.textContent = slideMeta[0].emoji || '☰';
    }

    setupTitleSlide();
    setControlsForIndex(0);
    startTitleHints();
    buildSlidePicker();
    applyDynamicScaling();
    setTimeout(applyDynamicScaling, 50);
}


// Initialize scroll behavior
function initHorizontalScroll() {
    if (!slidesWrapper) return;

    // Reset slide indices
    activeSlideIndex = 0;
    targetSlideIndex = 0;

    // Set initial colors
    updateColors();

    // Scroll event handler (rAF throttled) for programmatic changes/drag
    let scrollScheduled = false;
    slidesWrapper.addEventListener('scroll', () => {
        if (scrollScheduled) return;
        scrollScheduled = true;
        requestAnimationFrame(() => {
            scrollScheduled = false;
            const slideWidth = slidesWrapper.offsetWidth || 1;
            const scrollLeft = slidesWrapper.scrollLeft;
            updateColors(scrollLeft, slideWidth);
            checkSlideChange(scrollLeft, slideWidth);
        });
    });

    // Drag handling - clean implementation
    let dragState = {
        active: false,
        startX: 0,
        startScroll: 0,
        moved: false
    };

    slidesWrapper.addEventListener('mousedown', (e) => {
        dragState.active = true;
        dragState.startX = e.pageX;
        dragState.startScroll = slidesWrapper.scrollLeft;
        dragState.moved = false;
        slidesWrapper.style.cursor = 'grabbing';
        // Disable scroll-snap during drag for smooth tracking
        slidesWrapper.style.scrollSnapType = 'none';
        slidesWrapper.style.scrollBehavior = 'auto';
    });

    document.addEventListener('mousemove', (e) => {
        if (!dragState.active) return;

        const delta = dragState.startX - e.pageX;
        slidesWrapper.scrollLeft = dragState.startScroll + delta;

        if (Math.abs(delta) > 5) {
            dragState.moved = true;
        }
    });

    document.addEventListener('mouseup', () => {
        if (dragState.active) {
            dragState.active = false;
            slidesWrapper.style.cursor = 'grab';

            const slideWidth = slidesWrapper.offsetWidth;
            const currentScroll = slidesWrapper.scrollLeft;
            const startIndex = Math.round(dragState.startScroll / slideWidth);
            const delta = currentScroll - dragState.startScroll;
            const threshold = slideWidth * 0.25;

            let targetIndex = startIndex;
            if (delta > threshold) {
                targetIndex = Math.min(startIndex + 1, slideElements.length - 1);
            } else if (delta < -threshold) {
                targetIndex = Math.max(startIndex - 1, 0);
            } else {
                // if moved less than threshold, snap back to nearest based on current scroll
                targetIndex = Math.round(currentScroll / slideWidth);
            }

            slidesWrapper.scrollTo({
                left: targetIndex * slideWidth,
                behavior: 'smooth'
            });

            // Restore snap immediately after scheduling the scroll
            requestAnimationFrame(() => {
                slidesWrapper.style.scrollSnapType = 'x mandatory';
                slidesWrapper.style.scrollBehavior = 'smooth';
            });
        }
    });

    // Click to advance (only if didn't drag)
    slidesWrapper.addEventListener('click', (e) => {
        if (!dragState.moved) {
            targetSlideIndex = Math.min(targetSlideIndex + 1, slideElements.length - 1);
            scrollToSlide(targetSlideIndex);
        }
        dragState.moved = false;
    });

    // Arrow key navigation
    document.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight') {
            e.preventDefault();
            targetSlideIndex = Math.min(targetSlideIndex + 1, slideElements.length - 1);
            scrollToSlide(targetSlideIndex);
        } else if (e.key === 'ArrowLeft') {
            e.preventDefault();
            targetSlideIndex = Math.max(targetSlideIndex - 1, 0);
            scrollToSlide(targetSlideIndex);
        }
    });

    // Window resize handling
    let resizeTimeout = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimeout);
        resizeTimeout = setTimeout(() => {
            const slideWidth = slidesWrapper.offsetWidth;
            slidesWrapper.scrollTo({ left: targetSlideIndex * slideWidth, behavior: 'auto' });
            applyDynamicScaling();
            restartSlideEffect(targetSlideIndex);
        }, 150);
    });

    // Start first slide effect
    restartSlideEffect(0);
    if (effectsEnabled) {
        switchEmojiEffect(0, true);
    }
}

function scrollToSlide(index) {
    if (!slidesWrapper) return;
    const slideWidth = slidesWrapper.offsetWidth;
    slidesWrapper.scrollTo({
        left: index * slideWidth,
        behavior: 'smooth'
    });
    setControlsForIndex(index);
}

function updateColors(scrollLeftOverride, slideWidthOverride) {
    if (!slidesWrapper || slideElements.length === 0) return;

    const scrollLeft = (scrollLeftOverride !== undefined ? scrollLeftOverride : slidesWrapper.scrollLeft) || 0;
    const slideWidth = slideWidthOverride || slidesWrapper.offsetWidth || 1;
    const scrollRatio = scrollLeft / slideWidth;
    const currentIndex = Math.floor(scrollRatio);
    const nextIndex = Math.min(currentIndex + 1, slideElements.length - 1);
    const blendRatio = scrollRatio - currentIndex;

    const currentSlide = slideElements[currentIndex];
    const nextSlide = slideElements[nextIndex];

    const blendedBg = blendColorsHSV(
        currentSlide.dataset.bg,
        nextSlide.dataset.bg,
        blendRatio
    );

    const blendedFg = blendColorsHSV(
        currentSlide.dataset.fg,
        nextSlide.dataset.fg,
        blendRatio
    );

    const blendedAc = blendColorsHSV(
        currentSlide.dataset.ac,
        nextSlide.dataset.ac,
        blendRatio
    );

    // Set CSS custom properties for use in stylesheets
    document.body.style.setProperty('--bg', blendedBg);
    document.body.style.setProperty('--fg', blendedFg);
    document.body.style.setProperty('--text', blendedFg);
    document.body.style.setProperty('--ac', blendedAc);

    const rootStyle = document.documentElement.style;
    rootStyle.setProperty('--bg', blendedBg);
    rootStyle.setProperty('--fg', blendedFg);
    rootStyle.setProperty('--text', blendedFg);
    rootStyle.setProperty('--ac', blendedAc);

    // Keep the variables scoped on the recap container too (in case defaults are overridden elsewhere)
    const recapContainer = document.querySelector('.actual-recap');
    if (recapContainer) {
        recapContainer.style.setProperty('--bg', blendedBg);
        recapContainer.style.setProperty('--fg', blendedFg);
        recapContainer.style.setProperty('--text', blendedFg);
        recapContainer.style.setProperty('--ac', blendedAc);
    }

    // Keep variables at the scroll container level too
    slidesWrapper.style.setProperty('--bg', blendedBg);
    slidesWrapper.style.setProperty('--fg', blendedFg);
    slidesWrapper.style.setProperty('--text', blendedFg);
    slidesWrapper.style.setProperty('--ac', blendedAc);

    // Also set background directly
    document.body.style.background = blendedBg;
}

function checkSlideChange(scrollLeftOverride, slideWidthOverride) {
    if (!slidesWrapper || slideElements.length === 0) return;

    const slideWidth = slideWidthOverride || slidesWrapper.offsetWidth || 1;
    const scrollLeft = (scrollLeftOverride !== undefined ? scrollLeftOverride : slidesWrapper.scrollLeft) || 0;

    const scrollRatio = scrollLeft / slideWidth;
    const currentIndex = Math.floor(scrollRatio);
    const fraction = scrollRatio - currentIndex;

    const visibilities = new Array(slideElements.length).fill(0);
    const clampedFraction = Math.min(Math.max(fraction, 0), 1);
    const currentVis = 1 - clampedFraction;
    const nextVis = clampedFraction;

    if (currentIndex >= 0 && currentIndex < visibilities.length) {
        visibilities[currentIndex] = currentVis;
    }
    if (currentIndex + 1 < visibilities.length) {
        visibilities[currentIndex + 1] = nextVis;
    }

    let mostVisibleIndex = 0;
    let highestVisibility = 0;
    visibilities.forEach((v, idx) => {
        if (v > highestVisibility || (v === highestVisibility && idx > mostVisibleIndex)) {
            highestVisibility = v;
            mostVisibleIndex = idx;
        }
    });

    // Start the animation once a slide is at least 70% visible
    if (highestVisibility >= 0.7 && mostVisibleIndex !== activeSlideIndex) {
        activeSlideIndex = mostVisibleIndex;
        targetSlideIndex = mostVisibleIndex;
        restartSlideEffect(mostVisibleIndex);
        setControlsForIndex(mostVisibleIndex);
        if (mostVisibleIndex === 0) {
            startTitleHints();
        } else {
            stopTitleHints();
        }
    }

    if (highestVisibility >= 0.5 && mostVisibleIndex !== activeSlideIndex) {
        updatePickerLabel(mostVisibleIndex);
    }

    // Swap emoji effects once the new slide is at least 50% visible
    if (effectsEnabled && highestVisibility >= 0.5 && mostVisibleIndex !== activeEmojiIndex) {
        switchEmojiEffect(mostVisibleIndex);
    }

    // Reset slides that are no longer dominant once another slide is past 80% visibility
    if (highestVisibility >= 0.8) {
        visibilities.forEach((visibility, index) => {
            if (index !== mostVisibleIndex && visibility < 0.8) {
                resetSlideEffect(index);
            }
        });
    }
}

function resetSlideEffect(index) {
    if (!slideElements[index]) return;

    const slide = slideElements[index];
    const bigElement = slide.querySelector('.slide-big');
    if (!bigElement) return;

    const type = bigElement.dataset.type;
    const effectData = bigElement.dataset.effect;

    if (!effectData) return;

    const effect = JSON.parse(effectData);

    // Reset to starting state
    if (type === 'num_countup' || type === 'countup') {
        const startNum = substituteVariables(String(effect.start_num || 0), recapData);
        bigElement.textContent = startNum;
    } else if (type === 'text_reveal') {
        const firstOption = effect.reveal_scroll_options?.[0] || '';
        bigElement.textContent = firstOption;
    } else if (type === 'list') {
        // Reset to encrypted state with random crypto chars
        const cryptoChars = effect.crypto_chars || '▓▒░█▄▀▌│┤┐└┴┬├─┼╔╗╚╝║═╬';
        const randomCharFor = (targetChar) =>
            /[A-Za-z0-9]/.test(targetChar)
                ? cryptoChars[Math.floor(Math.random() * cryptoChars.length)]
                : targetChar;
        const items = bigElement.querySelectorAll('.list-item');
        items.forEach(item => {
            const titleEl = item.querySelector('.list-item-title');
            const subtitleEl = item.querySelector('.list-item-subtitle');
            const titleText = item.dataset.titleText || '';
            const subtitleText = item.dataset.subtitleText || '';

            titleEl.textContent = Array.from(titleText).map((ch) => randomCharFor(ch)).join('');

            subtitleEl.textContent = Array.from(subtitleText).map((ch) => randomCharFor(ch)).join('');
        });
    }
}

function restartSlideEffect(index) {
    if (!slideElements[index]) return;

    const slide = slideElements[index];
    const bigElement = slide.querySelector('.slide-big');
    if (!bigElement) return;

    const type = bigElement.dataset.type;
    const effectData = bigElement.dataset.effect;

    if (!effectData) return;

    const effect = JSON.parse(effectData);

    if (type === 'num_countup' || type === 'countup') {
        runCountUpEffect(bigElement, effect, recapData);
    } else if (type === 'text_reveal') {
        runTextRevealEffect(bigElement, effect, recapData);
    } else if (type === 'list') {
        runCryptoRevealEffect(bigElement, effect);
    }
}

function switchEmojiEffect(targetIndex, immediate = false) {
    if (!slideElements[targetIndex]) return;
    if (!emojiOverlay) return;
    if (!effectsEnabled) {
        stopEmojiAnimation();
        fadeOutEmojiOverlay();
        activeEmojiIndex = null;
        return;
    }
    if (targetIndex === activeEmojiIndex && !immediate) return;

    clearTimeout(emojiSwitchTimeout);

    const slide = slideElements[targetIndex];
    const effect = (slide.dataset.emjEffect || '').toLowerCase();
    const emjString = slide.dataset.emj || '';

    const startNew = () => {
        if (effect === 'rain' && emjString.trim()) {
            startEmojiRain(emjString, immediate);
            activeEmojiIndex = targetIndex;
        } else if (effect === 'fireworks' && emjString.trim()) {
            startEmojiFireworks(emjString, immediate);
            activeEmojiIndex = targetIndex;
        } else if (effect === 'stars') {
            startStarfield(immediate);
            activeEmojiIndex = targetIndex;
        } else {
            stopEmojiAnimation();
            fadeOutEmojiOverlay();
            activeEmojiIndex = null;
        }
    };

    if (immediate) {
        startNew();
        return;
    }

    fadeOutEmojiOverlay();
    startNew();
}

// Main render function
async function showRecapSlides(backendData) {
    document.querySelector(".existing-recap").classList.add("hidden");
    document.querySelector(".generating-recap").classList.add("hidden");

    if (backendData?.share_images?.grid) {
        shareImageUrl = normalizeShareImageUrl(backendData.share_images.grid);
    }

    try {
        // Load recap-style.json
        const response = await fetch('/static/recap-style.json');
        const config = await response.json();

        // Build slides from config
        buildSlideElements(config.cards, backendData);

        // Initialize scroll behavior
        initHorizontalScroll();

        // Show container
        document.querySelector(".actual-recap").classList.remove("hidden");
    } catch (error) {
        console.error('Failed to render recap:', error);
        const recapContainer = document.getElementById("recap-container");
        recapContainer.innerHTML = "<div class='card'><h1 style='color: black;'>Failed to load recap</h1><p>" + error.message + "</p></div>";
        document.querySelector(".actual-recap").classList.remove("hidden");
    }
}

// Initialize on page load
document.addEventListener("DOMContentLoaded", init_page);
document.addEventListener('mouseover', (e) => {
    const target = e.target.closest('.controls-icon[data-tooltip]');
    if (!target) return;
    const text = target.getAttribute('data-tooltip');
    if (text) {
        showControlsTooltip(target, text, 0);
    }
});
document.addEventListener('mouseout', (e) => {
    const target = e.target.closest('.controls-icon[data-tooltip]');
    if (!target) return;
    hideControlsTooltip();
});


// functions for button
function prevslide() {
    if (!slidesWrapper || slideElements.length === 0) return;
    targetSlideIndex = Math.max(targetSlideIndex - 1, 0);
    scrollToSlide(targetSlideIndex);
}

function nextslide() {
    if (!slidesWrapper || slideElements.length === 0) return;
    targetSlideIndex = Math.min(targetSlideIndex + 1, slideElements.length - 1);
    scrollToSlide(targetSlideIndex);
}

function drawRainParticles(ctx, emjString, width, height) {
    const chars = Array.from(emjString || '').filter((c) => c.trim().length > 0);
    if (chars.length === 0) return;
    const count = 48;
    for (let i = 0; i < count; i++) {
        const ch = chars[Math.floor(Math.random() * chars.length)];
        const x = Math.random() * width;
        const y = Math.random() * height;
        const size = 46 + Math.random() * 30;
        const rot = (Math.random() * 40 - 20) * Math.PI / 180;
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(rot);
        ctx.font = `${size}px sans-serif`;
        ctx.globalAlpha = 0.8;
        ctx.fillText(ch, 0, 0);
        ctx.restore();
    }
}

function drawFireworksParticles(ctx, emjString, width, height) {
    const chars = Array.from(emjString || '').filter((c) => c.trim().length > 0);
    if (chars.length === 0) return;
    const bursts = 3;
    for (let b = 0; b < bursts; b++) {
        const cx = width * (0.2 + Math.random() * 0.6);
        const cy = height * (0.25 + Math.random() * 0.45);
        const parts = 10 + Math.floor(Math.random() * 8);
        for (let i = 0; i < parts; i++) {
            const ch = chars[Math.floor(Math.random() * chars.length)];
            const angle = Math.random() * Math.PI * 2;
            const radius = 80 + Math.random() * 140;
            const x = cx + Math.cos(angle) * radius;
            const y = cy + Math.sin(angle) * radius;
            const size = 42 + Math.random() * 24;
            ctx.save();
            ctx.translate(x, y);
            ctx.rotate(Math.random() * Math.PI * 2);
            ctx.font = `${size}px sans-serif`;
            ctx.globalAlpha = 0.9;
            ctx.fillText(ch, 0, 0);
            ctx.restore();
        }
    }
}

function drawStars(ctx, width, height) {
    const count = 140;
    ctx.save();
    for (let i = 0; i < count; i++) {
        const x = Math.random() * width;
        const y = Math.random() * height;
        const r = 1 + Math.random() * 2;
        const opacity = 0.3 + Math.random() * 0.7;
        ctx.beginPath();
        ctx.fillStyle = `rgba(255,255,255,${opacity.toFixed(2)})`;
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
    }
    ctx.restore();
}

function renderSlideToCanvas(index) {
    const canvas = document.getElementById('vert-image-canvas');
    if (!canvas || !slideElements[index]) return null;
    const ctx = canvas.getContext('2d');
    const slide = slideElements[index];
    const { bg, fg, ac } = getCurrentTheme();
    const scale = 2;
    const width = canvas.width;
    const height = canvas.height;
    const w = width / scale;
    const h = height / scale;

    ctx.save();
    ctx.clearRect(0, 0, width, height);
    ctx.scale(scale, scale);

    ctx.fillStyle = bg || slide.dataset.bg || '#000';
    ctx.fillRect(0, 0, w, h);

    if (effectsEnabled) {
        const effect = (slide.dataset.emjEffect || '').toLowerCase();
        const emjString = slide.dataset.emj || '';
        if (effect === 'rain') {
            drawRainParticles(ctx, emjString, w, h);
        } else if (effect === 'fireworks') {
            drawFireworksParticles(ctx, emjString, w, h);
        } else if (effect === 'stars') {
            drawStars(ctx, w, h);
        }
    }

    const title = slide.querySelector('.slide-title')?.textContent || '';
    const big = slide.querySelector('.slide-big')?.textContent || '';
    const desc = slide.querySelector('.slide-desc')?.textContent || '';
    const name = (recapData?.user_name || 'Your') + "'s Recap";

    ctx.textAlign = 'center';
    ctx.fillStyle = fg || '#fff';

    // Header name
    ctx.font = '700 64px sans-serif';
    ctx.fillText(name, w / 2, h * 0.12);

    ctx.font = '600 54px sans-serif';
    ctx.fillText(title, w / 2, h * 0.28);

    ctx.fillStyle = ac || fg || '#fff';
    ctx.font = '800 150px sans-serif';
    ctx.fillText(big, w / 2, h * 0.5);

    ctx.fillStyle = fg || '#fff';
    ctx.font = '400 42px sans-serif';
    ctx.fillText(desc, w / 2, h * 0.65);

    ctx.restore();

    return canvas.toDataURL('image/png');
}

function downloadCurrentSlide() {
    if (shareImageUrl) {
        const link = document.createElement('a');
        link.download = `recap-${RECAP_ID || 'grid'}.png`;
        link.href = shareImageUrl;
        link.click();
        return;
    }
    if (!slideElements.length) return;
    const dataUrl = renderSlideToCanvas(activeSlideIndex || 0);
    if (!dataUrl) return;
    const link = document.createElement('a');
    link.download = `recap-slide-${(activeSlideIndex || 0) + 1}.png`;
    link.href = dataUrl;
    link.click();
}

let controlsTooltipEl = null;
let controlsTooltipTimeout = null;

function ensureControlsTooltip() {
    if (controlsTooltipEl) return controlsTooltipEl;
    const el = document.createElement('div');
    el.className = 'controls-tooltip-layer';
    document.body.appendChild(el);
    controlsTooltipEl = el;
    return el;
}

function hideControlsTooltip() {
    if (controlsTooltipTimeout) {
        clearTimeout(controlsTooltipTimeout);
        controlsTooltipTimeout = null;
    }
    if (controlsTooltipEl) {
        controlsTooltipEl.classList.remove('visible');
    }
}

function showControlsTooltip(target, text, duration = 1600) {
    if (!target) return;
    const tooltip = ensureControlsTooltip();
    tooltip.textContent = text;
    tooltip.style.width = 'auto';
    tooltip.style.maxWidth = 'none';
    // Measure natural width first to clamp precisely to text
    tooltip.style.visibility = 'hidden';
    const rect = target.getBoundingClientRect();
    // Position tooltip centered above target, but constrained to viewport
    const padding = 10;
    const measuredWidth = Math.ceil(tooltip.scrollWidth) || rect.width;
    const maxWidth = Math.min(360, window.innerWidth - padding * 2);
    const tooltipWidth = Math.min(measuredWidth, maxWidth);
    tooltip.style.width = `${tooltipWidth}px`;
    tooltip.style.maxWidth = `${maxWidth}px`;
    const left = Math.min(
        window.innerWidth - tooltipWidth - padding,
        Math.max(padding, rect.left + rect.width / 2 - tooltipWidth / 2)
    );
    const tooltipHeight = tooltip.offsetHeight || 0;
    const top = Math.max(padding, rect.top - tooltipHeight - 12);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
    tooltip.style.visibility = '';
    tooltip.classList.add('visible');
    if (duration > 0) {
        if (controlsTooltipTimeout) clearTimeout(controlsTooltipTimeout);
        controlsTooltipTimeout = setTimeout(() => {
            hideControlsTooltip();
        }, duration);
    }
}

function updateControlsTooltip(target, text) {
    if (!target) return;
    target.setAttribute('data-tooltip', text);
    target.setAttribute('aria-label', text);
}

function copyShareLink(target) {
    // Share link if email is 28axu@pinewood.edu then https://recap.pinewood.one/s/28axu
    const email = recapData?.user_email || '';
    const shareLink = `https://recap.pinewood.one/s/${email.split('@')[0]}`;
    const tooltipTarget = target || document.getElementById('copy-share-link');

    const showCopiedTooltip = () => {
        showControlsTooltip(tooltipTarget, COPY_SHARE_TOOLTIP_SUCCESS, 1600);
        updateControlsTooltip(tooltipTarget, COPY_SHARE_TOOLTIP_SUCCESS);
        setTimeout(() => updateControlsTooltip(tooltipTarget, COPY_SHARE_TOOLTIP_DEFAULT), 1600);
    };

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(shareLink).then(showCopiedTooltip).catch(() => {
            updateControlsTooltip(tooltipTarget, COPY_SHARE_TOOLTIP_DEFAULT);
        });
    } else {
        const fallbackInput = document.createElement('input');
        fallbackInput.value = shareLink;
        fallbackInput.setAttribute('readonly', '');
        fallbackInput.style.position = 'absolute';
        fallbackInput.style.left = '-9999px';
        document.body.appendChild(fallbackInput);
        const selection = document.getSelection();
        const selectedRange = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
        fallbackInput.select();
        try {
            document.execCommand('copy');
            showCopiedTooltip();
        } catch (err) {
            console.error('Failed to copy share link', err);
            updateControlsTooltip(tooltipTarget, COPY_SHARE_TOOLTIP_DEFAULT);
        } finally {
            document.body.removeChild(fallbackInput);
            if (selectedRange && selection) {
                selection.removeAllRanges();
                selection.addRange(selectedRange);
            }
        }
    }
}
