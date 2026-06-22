/* ============================================
   文章考核系统 - 前端逻辑
   ============================================ */

// --- State ---
let currentStep = 1;
let sessionData = null;   // { session_id, title, summary, questions, answers }

// 全局变量，用于暂存从后端获取的完整配置
let fullConfig = null;

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    loadApiConfig();
    loadRecentSessions();

    // Char count
    const textarea = document.getElementById('article-text');
    textarea.addEventListener('input', () => {
        document.getElementById('char-count').textContent = textarea.value.length + ' 字';
    });
});

// --- API Helpers ---
async function api(method, path, body) {
    const opts = {
        method,
        headers: { 'Content-Type': 'application/json' },
    };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(path, opts);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '请求失败');
    return data;
}

// --- Toast ---
function showToast(message, type = 'info') {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const icons = { success: 'check-circle', error: 'exclamation-circle', info: 'info-circle' };
    toast.innerHTML = `<i class="fas fa-${icons[type] || 'info-circle'}"></i><span>${message}</span>`;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3500);
}

// --- Loading ---
function showLoading(title, text) {
    document.getElementById('loading-title').textContent = title || '正在处理';
    document.getElementById('loading-text').textContent = text || '请稍候...';
    document.getElementById('loading-overlay').classList.add('active');
}

function hideLoading() {
    document.getElementById('loading-overlay').classList.remove('active');
}

// --- Step Navigation ---
function goToStep(step) {
    currentStep = step;
    document.querySelectorAll('.step-section').forEach(s => s.classList.remove('active'));
    document.getElementById(`step-${step === 1 ? 'input' : step === 2 ? 'exam' : 'results'}`).classList.add('active');

    // Update stepper
    for (let i = 1; i <= 3; i++) {
        const el = document.getElementById(`stepper-${i}`);
        el.classList.remove('active', 'completed');
        if (i < step) el.classList.add('completed');
        if (i === step) el.classList.add('active');
    }
    for (let i = 1; i <= 2; i++) {
        const line = document.getElementById(`line-${i}`);
        line.classList.toggle('completed', i < step);
    }

    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// --- Input Tab ---
function switchInputTab(tab) {
    // 1. 切换 Tab 按钮的激活状态
    document.querySelectorAll('.input-tab').forEach(t => t.classList.remove('active'));
    const tabs = document.querySelectorAll('.input-tab');
    if (tab === 'text') {
        tabs[0].classList.add('active');
    } else {
        tabs[1].classList.add('active');
    }

    // 2. 切换输入区域的显示/隐藏
    const textArea = document.getElementById('input-text-area');
    const urlArea = document.getElementById('input-url-area');
    textArea.style.display = (tab === 'text') ? 'block' : 'none';
    urlArea.style.display = (tab === 'url') ? 'block' : 'none';

    // 3. 动态控制“及格分数”和“生成考题”组件
    const scoreConfig = document.getElementById('score-config-row');
    const generateBtnGroup = document.getElementById('generate-btn-group');

    if (tab === 'url') {
        // 当切换到“输入链接”时，隐藏这些组件
        scoreConfig.style.display = 'none';
        generateBtnGroup.style.display = 'none';
    } else {
        // 当切换到“粘贴文本”时，显示这些组件
        // 注意：使用 flex 或 block 取决于你 CSS 中的原始布局，通常 config-row 是 flex
        scoreConfig.style.display = 'flex';
        generateBtnGroup.style.display = 'flex';
    }
}

// --- Fetch URL ---
async function fetchUrl() {
    const url = document.getElementById('article-url').value.trim();
    if (!url) { showToast('请输入文章链接', 'error'); return; }

    const btn = document.getElementById('fetch-url-btn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 抓取中...';

    try {
        showLoading('正在抓取', '正在获取文章内容...');
        const data = await api('GET', `/api/fetch-url?url=${encodeURIComponent(url)}`);
        document.getElementById('article-text').value = data.content;
        document.getElementById('char-count').textContent = data.word_count + ' 字';
        showToast(`成功抓取 ${data.word_count} 字`, 'success');
        // Switch to text tab
        switchInputTab('text');
        document.querySelectorAll('.input-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.input-tab')[0].classList.add('active');
    } catch (e) {
        showToast(e.message, 'error');
    } finally {
        hideLoading();
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-download"></i> 抓取文章内容';
    }
}

// --- Generate Exam ---
async function generateExam() {
    const text = document.getElementById('article-text').value.trim();
    if (!text) { showToast('请输入文章内容', 'error'); return; }
    if (text.length < 100) { showToast('文章内容至少需要 100 字', 'error'); return; }

    const passScore = parseInt(document.getElementById('pass-score').value) || 60;
    
    // 🔑 获取分阶段模型索引
    const extractIdx = parseInt(document.getElementById('extract-model-select')?.value || '-1');
    const generateIdx = parseInt(document.getElementById('generate-model-select')?.value || '-1');

    const btn = document.getElementById('generate-btn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 生成中...';

    try {
        showLoading('AI 正在分析文章', '正在生成考核题目，请稍候...');
        const data = await api('POST', '/api/generate', {
            article_text: text,
            pass_score: passScore,
            extract_model_index: extractIdx >= 0 ? extractIdx : null,   // 🔑 提取阶段模型
            generate_model_index: generateIdx >= 0 ? generateIdx : null // 🔑 出题阶段模型
        });

        sessionData = {
            session_id: data.session_id,
            title: data.title,
            summary: data.summary,
            questions: data.questions,
            answers: {},
            pass_score: passScore,
        };

        renderExam();
        goToStep(2);
        showToast(`已生成 ${data.total} 道题目`, 'success');
    } catch (e) {
        showToast(e.message, 'error');
    } finally {
        hideLoading();
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-magic"></i> 生成考题';
    }
}

// --- Render Exam ---
function renderExam() {
    if (!sessionData) return;

    document.getElementById('exam-title').textContent = sessionData.title;
    document.getElementById('exam-summary').textContent = sessionData.summary || '';
    document.getElementById('total-count').textContent = sessionData.questions.length;

    const container = document.getElementById('questions-container');
    container.innerHTML = '';

    sessionData.questions.forEach((q, idx) => {
        const card = document.createElement('div');
        card.className = 'question-card';
        card.id = `question-${q.id}`;

        // 认知层级徽章
        const levelBadge = getCognitiveLevelBadge(q.cognitive_level);
        
        // 题型徽章
        const typeBadge = getQuestionTypeBadge(q.question_type);

        let bodyHTML = '';
        let questionTextHTML = '';
                
        // 根据题型渲染不同的作答界面
        switch (q.question_type) {
            case 'fill_blank':
                // 填空题：由 renderFillBlank 处理题目文本中的下划线
                bodyHTML = renderFillBlank(q, idx);
                break;
            case 'true_false':
                questionTextHTML = escapeHtml(q.question);
                bodyHTML = renderTrueFalse(q, idx);
                break;
            case 'single_choice':
            case 'multiple_choice':
                questionTextHTML = escapeHtml(q.question);
                bodyHTML = renderChoice(q, idx);
                break;
            case 'short_answer':
            case 'case_analysis':
            case 'open_ended':
            case 'debate':
            case 'design':
                questionTextHTML = escapeHtml(q.question);
                bodyHTML = renderShortAnswer(q, idx);
                break;
            default:
                questionTextHTML = escapeHtml(q.question);
                bodyHTML = `<p style="color:var(--danger)">未知题型：${q.question_type}</p>`
        }
        
        card.innerHTML = `
            <div class="question-top">
                <div class="question-number">${idx + 1}</div>
                ${levelBadge}
                ${typeBadge}
            </div>
            ${questionTextHTML ? `<div class="question-text">${questionTextHTML}</div>` : ''}
            ${bodyHTML}`;

        container.appendChild(card);
    });

    updateProgress();
}

// --- Answer Handlers ---

// 渲染认知层级徽章
function getCognitiveLevelBadge(level) {
    const badges = {
        'basic': '<span class="question-type-badge" style="background:var(--info-light);color:var(--info)">基础认知</span>',
        'apply': '<span class="question-type-badge" style="background:var(--warning-light);color:#b45309">应用分析</span>',
        'advanced': '<span class="question-type-badge" style="background:var(--danger-light);color:var(--danger)">高阶思维</span>'
    };
    return badges[level] || '';
}

// 渲染题型徽章
function getQuestionTypeBadge(type) {
    const badges = {
        'fill_blank': '填空题',
        'true_false': '判断题',
        'single_choice': '单选题',
        'multiple_choice': '多选题',
        'short_answer': '简答题',
        'case_analysis': '案例分析',
        'open_ended': '开放问答',
        'debate': '辩论题',
        'design': '设计题'
    };
    const text = badges[type] || type;
    const className = ['single_choice', 'multiple_choice', 'true_false'].includes(type) ? 'badge-choice' : 'badge-short';
    return `<span class="question-type-badge ${className}">${text}</span>`;
}

// 渲染填空题 - 在题目文本的划线处直接填写
function renderFillBlank(q, idx) {
    const saved = sessionData.answers[String(q.id)] || '';
    const savedAnswers = saved.split('|').map(s => s.trim());  // 从保存的数据中分割多个答案
    
    // 检查题目是否包含下划线标记（______）
    const questionText = q.question;
    const blankRegex = /_{2,}/g;  // 匹配连续的下划线
    const hasBlank = blankRegex.test(questionText);
    
    if (hasBlank) {
        // 将下划线替换为输入框
        const parts = questionText.split(blankRegex);
        let html = '';
        let blankIndex = 0;  // 空格索引
        
        parts.forEach((part, i) => {
            html += `<span>${escapeHtml(part)}</span>`;
            if (i < parts.length - 1) {
                // 获取已保存的答案
                const inputValue = savedAnswers[blankIndex] || '';
                
                // 🔑 关键改进：使用占位符属性存储最小宽度，由 CSS 和 JS 共同控制宽度
                html += `<input type="text" class="fill-blank-input" 
                    data-qid="${q.id}" 
                    data-index="${blankIndex}"
                    value="${escapeHtml(inputValue)}"
                    data-min-chars="3"
                    placeholder="在此填写"
                    oninput="onFillBlankMulti(${q.id}, this); autoResizeInput(this)" />`;
                
                blankIndex++;
            }
        });
        
        // 🔑 修复历史记录恢复时的宽度问题：渲染完成后立即调整所有空格宽度
        setTimeout(() => {
            const container = document.querySelector(`#question-${q.id} .fill-blank-container`);
            if (container) {
                container.querySelectorAll('.fill-blank-input').forEach(input => {
                    autoResizeInput(input);
                });
            }
        }, 0);

        return `<div class="fill-blank-container">${html}</div>`;
    } else {
        // 如果没有下划线，使用传统文本框
        return `
            <textarea class="short-answer-input" id="answer-${q.id}"
                placeholder="请输入填空答案..."
                oninput="onShortAnswer(${q.id}, this.value)">${escapeHtml(saved)}</textarea>`;
    }
}

// 填空题输入处理（支持多个空）
function onFillBlankMulti(qId, inputEl) {
    const container = document.getElementById(`question-${qId}`);
    const inputs = container.querySelectorAll('.fill-blank-input');
    const answers = [];
    
    // 收集所有输入框的值
    inputs.forEach(input => {
        answers.push(input.value.trim());
    });
    
    // 用 | 分隔多个答案
    sessionData.answers[String(qId)] = answers.join('|');
    
    // 检查是否已作答（至少有一个空填了）
    const hasAnswer = answers.some(a => a.trim().length > 0);
    const card = container.closest('.question-card');
    if (card) {
        card.classList.toggle('answered', hasAnswer);
    }
    
    updateProgress();
}

// 🔑 自动调整输入框宽度（根据内容长度）
function autoResizeInput(input) {
    // 创建一个临时元素来测量文本宽度
    const testSpan = document.createElement('span');
    testSpan.style.cssText = `
        visibility: hidden;
        position: absolute;
        white-space: pre;
        font-family: inherit;
        font-size: inherit;
        font-weight: inherit;
    `;
    
    // 获取最小字符数（从 data 属性）
    const minChars = parseInt(input.getAttribute('data-min-chars')) || 3;
    
    // 使用当前值或 placeholder 作为测量内容
    const content = input.value || input.placeholder;
    // 确保至少有 minChars 个字符的宽度
    const measuredContent = content.length >= minChars ? content : '_'.repeat(minChars);
    
    testSpan.textContent = measuredContent;
    document.body.appendChild(testSpan);
    
    // 计算宽度（文本宽度 + 额外空间）
    const textWidth = testSpan.offsetWidth;
    const extraSpace = 16; // 左右各 8px 的 padding
    
    // 设置输入框宽度
    input.style.width = (textWidth + extraSpace) + 'px';
    
    // 清理临时元素
    document.body.removeChild(testSpan);
}

// 渲染判断题
function renderTrueFalse(q, idx) {
    const saved = sessionData.answers[String(q.id)];
    return `
        <div class="options-grid" style="grid-template-columns:1fr 1fr">
            <div class="option-item${saved === '正确' ? ' selected' : ''}" onclick="selectOption(${q.id}, '正确', this)">
                <div class="option-radio"></div>
                <span class="option-label">✓</span>
                <span class="option-text">正确</span>
            </div>
            <div class="option-item${saved === '错误' ? ' selected' : ''}" onclick="selectOption(${q.id}, '错误', this)">
                <div class="option-radio"></div>
                <span class="option-label">✗</span>
                <span class="option-text">错误</span>
            </div>
        </div>`;
}

// 渲染选择题（单选/多选）
function renderChoice(q, idx) {
    let bodyHTML = '<div class="options-grid">';
    for (const [key, val] of Object.entries(q.options)) {
        const saved = sessionData.answers[String(q.id)];
        // 支持多选（用逗号分隔）或单选
        let isSelected = false;
        if (q.question_type === 'multiple_choice') {
            const selections = (saved || '').split(',').map(s => s.trim().toUpperCase());
            isSelected = selections.includes(key.toUpperCase());
        } else {
            isSelected = saved === key;
        }
        
        const selectedClass = isSelected ? ' selected' : '';
        const clickHandler = q.question_type === 'multiple_choice' 
            ? `toggleMultipleOption(${q.id}, '${key}', this)` 
            : `selectOption(${q.id}, '${key}', this)`;
        
        bodyHTML += `
            <div class="option-item${selectedClass}" onclick="${clickHandler}">
                <div class="option-radio"></div>
                <span class="option-label">${key}.</span>
                <span class="option-text">${escapeHtml(val)}</span>
            </div>`;
    }
    bodyHTML += '</div>';
    return bodyHTML;
}

// 渲染简答题/案例分析/开放问答/辩论/设计题
function renderShortAnswer(q, idx) {
    const saved = sessionData.answers[String(q.id)] || '';
    const placeholders = {
        'short_answer': '请输入你的答案...',
        'case_analysis': '请分析案例并给出解决方案...',
        'open_ended': '请阐述你的观点和理由...',
        'debate': '请陈述你的论点和论据...',
        'design': '请描述你的设计方案...'
    };
    const placeholder = placeholders[q.question_type] || '请输入你的答案...';
    return `
        <textarea class="short-answer-input" id="answer-${q.id}"
            placeholder="${placeholder}"
            oninput="onShortAnswer(${q.id}, this.value)">${escapeHtml(saved)}</textarea>`;
}

function selectOption(qId, option, el) {
    sessionData.answers[String(qId)] = option;

    // Update UI
    const card = document.getElementById(`question-${qId}`);
    card.querySelectorAll('.option-item').forEach(o => o.classList.remove('selected'));
    el.classList.add('selected');
    card.classList.add('answered');

    updateProgress();
}

// 多选题切换选项
function toggleMultipleOption(qId, option, el) {
    let current = sessionData.answers[String(qId)] || '';
    let selections = current.split(',').map(s => s.trim().toUpperCase()).filter(s => s);
    
    const idx = selections.indexOf(option.toUpperCase());
    if (idx >= 0) {
        // 取消选择
        selections.splice(idx, 1);
    } else {
        // 添加选择
        selections.push(option.toUpperCase());
    }
    
    sessionData.answers[String(qId)] = selections.join(',');
    
    // Update UI
    const card = document.getElementById(`question-${qId}`);
    card.querySelectorAll('.option-item').forEach(o => {
        const label = o.querySelector('.option-label').textContent.replace('.', '').trim();
        const isSelected = selections.includes(label.toUpperCase());
        o.classList.toggle('selected', isSelected);
    });
    card.classList.add('answered');
    
    updateProgress();
}

function onShortAnswer(qId, value) {
    sessionData.answers[String(qId)] = value;
    const card = document.getElementById(`question-${qId}`);
    card.classList.toggle('answered', value.trim().length > 0);
    updateProgress();
}

function updateProgress() {
    if (!sessionData) return;
    const total = sessionData.questions.length;
    const answered = Object.values(sessionData.answers).filter(a => a && a.trim()).length;
    document.getElementById('answered-count').textContent = answered;
    const pct = Math.round((answered / total) * 100);
    document.getElementById('progress-fill').style.width = pct + '%';
    saveProgress();
}

// --- Save Progress ---
async function saveProgress() {
    if (!sessionData) return;
    try {
        await api('POST', '/api/save', {
            session_id: sessionData.session_id,
            answers: sessionData.answers,
        });
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}

// --- Submit Exam ---
async function submitExam() {
    if (!sessionData) return;

    const total = sessionData.questions.length;
    const answered = Object.values(sessionData.answers).filter(a => a && a.trim()).length;

    if (answered < total) {
        const unanswered = total - answered;
        if (!confirm(`还有 ${unanswered} 道题未作答，确定要提交吗？`)) return;
    }

    const btn = document.getElementById('submit-btn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 批改中...';

    try {
        showLoading('AI 正在批改', '正在评估你的答卷，请稍候...');
        const data = await api('POST', '/api/grade', {
            session_id: sessionData.session_id,
            questions: sessionData.questions,
            answers: sessionData.answers,
            pass_score: sessionData.pass_score,
        });

        renderResults(data);
        goToStep(3);
    } catch (e) {
        showToast('批改失败: ' + e.message, 'error');
    } finally {
        hideLoading();
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-paper-plane"></i> 提交答卷';
    }
}

// --- Render Results ---
function renderResults(data) {
    const { score, passed, pass_score, details } = data;

    // Score ring
    const circumference = 2 * Math.PI * 52; // ~326.73
    const offset = circumference - (score / 100) * circumference;
    const ringFill = document.getElementById('score-ring-fill');

    // Color based on score
    let color;
    if (passed) color = 'var(--success)';
    else if (score >= pass_score - 10) color = 'var(--warning)';
    else color = 'var(--danger)';

    ringFill.style.stroke = color;
    ringFill.style.strokeDasharray = circumference;

    // Animate
    setTimeout(() => {
        ringFill.style.strokeDashoffset = offset;
    }, 100);

    // Score number animation
    const scoreEl = document.getElementById('final-score');
    scoreEl.style.color = color;
    animateNumber(scoreEl, 0, score, 1200);

    // Pass badge
    const badge = document.getElementById('pass-badge');
    if (passed) {
        badge.className = 'pass-badge passed';
        badge.innerHTML = '<i class="fas fa-check-circle"></i> 恭喜通过考核';
    } else {
        badge.className = 'pass-badge failed';
        badge.innerHTML = '<i class="fas fa-times-circle"></i> 未达到及格线';
    }

    // Summary - 统计各题型
    const typeStats = {};
    details.forEach(d => {
        const type = getQuestionTypeLabel(d.question_type);
        if (!typeStats[type]) typeStats[type] = { correct: 0, total: 0 };
        typeStats[type].total++;
        if (d.score === d.max_score) typeStats[type].correct++;
    });
    
    let summaryText = Object.entries(typeStats)
        .map(([type, stat]) => `${type} ${stat.correct}/${stat.total}`)
        .join(' · ');
    
    document.getElementById('results-summary').textContent = summaryText + ` · 及格线 ${pass_score} 分`;

    // Review cards
    const container = document.getElementById('review-container');
    container.innerHTML = '';

    details.forEach((d, idx) => {
        const card = document.createElement('div');
        card.className = 'review-card';

        let statusClass, statusIcon, scoreClass;
        const ratio = d.score / d.max_score;
        if (ratio >= 0.8) { statusClass = 'correct'; statusIcon = 'check'; scoreClass = 'full'; }
        else if (ratio >= 0.4) { statusClass = 'partial'; statusIcon = 'minus'; scoreClass = 'half'; }
        else { statusClass = 'wrong'; statusIcon = 'times'; scoreClass = 'zero'; }

        card.classList.add(statusClass);

        let answersHTML = '';
        
        // 根据题型显示不同的答案格式
        if (['single_choice', 'true_false'].includes(d.question_type)) {
            // 单选题/判断题：如果有 option_details，详细展示每个选项
            if (d.option_details && d.option_details.length > 0) {
                answersHTML = `<div class="review-options-grid">`;
                d.option_details.forEach(opt => {
                    const statusClass = {
                        'correctly_selected': 'option-correct',
                        'incorrectly_selected': 'option-wrong',
                        'missed': 'option-missed',
                        'not_selected': 'option-normal'
                    }[opt.status] || 'option-normal';
                    
                    const icon = {
                        'correctly_selected': '<i class="fas fa-check"></i>',
                        'incorrectly_selected': '<i class="fas fa-times"></i>',
                        'missed': '<i class="fas fa-minus-circle"></i>',
                        'not_selected': ''
                    }[opt.status] || '';
                    
                    answersHTML += `
                        <div class="review-option-item ${statusClass}">
                            <span class="option-key">${escapeHtml(opt.key)}.</span>
                            <span class="option-value">${escapeHtml(opt.value)}</span>
                            ${icon ? `<span class="option-icon">${icon}</span>` : ''}
                        </div>`;
                });
                answersHTML += `</div>`;
            } else {
                // 兼容旧格式
                answersHTML = `
                    <div class="review-answers">
                        <div class="review-answer-row">
                            <span class="review-answer-label correct-label">正确答案：</span>
                            <span class="review-answer-value">${d.correct_answer}</span>
                        </div>
                        <div class="review-answer-row">
                            <span class="review-answer-label user-label">你的选择：</span>
                            <span class="review-answer-value">${d.user_answer || '未作答'}</span>
                        </div>
                    </div>`;
            }
        } else if (d.question_type === 'multiple_choice') {
            // 多选题：使用 option_details 详细展示
            if (d.option_details && d.option_details.length > 0) {
                answersHTML = `<div class="review-options-grid">`;
                d.option_details.forEach(opt => {
                    const statusClass = {
                        'correctly_selected': 'option-correct',
                        'incorrectly_selected': 'option-wrong',
                        'missed': 'option-missed',
                        'not_selected': 'option-normal'
                    }[opt.status] || 'option-normal';
                    
                    const icon = {
                        'correctly_selected': '<i class="fas fa-check"></i>',
                        'incorrectly_selected': '<i class="fas fa-times"></i>',
                        'missed': '<i class="fas fa-minus-circle"></i>',
                        'not_selected': ''
                    }[opt.status] || '';
                    
                    answersHTML += `
                        <div class="review-option-item ${statusClass}">
                            <span class="option-key">${escapeHtml(opt.key)}.</span>
                            <span class="option-value">${escapeHtml(opt.value)}</span>
                            ${icon ? `<span class="option-icon">${icon}</span>` : ''}
                        </div>`;
                });
                answersHTML += `</div>`;
            } else {
                // 兼容旧格式
                const correctAns = Array.from(d.correct_answer || '').sort().join(',');
                const userAns = Array.from((d.user_answer || '').split(',')).sort().join(',');
                answersHTML = `
                    <div class="review-answers">
                        <div class="review-answer-row">
                            <span class="review-answer-label correct-label">正确答案：</span>
                            <span class="review-answer-value">${correctAns}</span>
                        </div>
                        <div class="review-answer-row">
                            <span class="review-answer-label user-label">你的选择：</span>
                            <span class="review-answer-value">${userAns || '未作答'}</span>
                        </div>
                    </div>`;
            }
        } else if (d.question_type === 'fill_blank') {
            // 填空题：只显示参考答案和用户答案，不需要原文依据
            answersHTML = `
                <div class="review-answers">
                    <div class="review-answer-row">
                        <span class="review-answer-label correct-label">参考答案：</span>
                        <span class="review-answer-value">${escapeHtml(d.correct_answer)}</span>
                    </div>
                    <div class="review-answer-row">
                        <span class="review-answer-label user-label">你的答案：</span>
                        <span class="review-answer-value">${escapeHtml(d.user_answer) || '<em style="color:var(--text-muted)">未作答</em>'}</span>
                    </div>
                </div>`;
        } else {
            // 主观题（简答、案例分析、开放问答等）
            answersHTML = `
                <div class="review-answers">
                    <div class="review-answer-row">
                        <span class="review-answer-label correct-label">参考答案：</span>
                        <span class="review-answer-value">${escapeHtml(d.correct_answer)}</span>
                    </div>
                    <div class="review-answer-row">
                        <span class="review-answer-label user-label">你的回答：</span>
                        <span class="review-answer-value">${escapeHtml(d.user_answer) || '<em style="color:var(--text-muted)">未作答</em>'}</span>
                    </div>
                    ${d.comment ? `<div class="review-answer-row">
                        <span class="review-answer-label user-label">AI 评语：</span>
                        <span class="review-answer-value">${escapeHtml(d.comment)}</span>
                    </div>` : ''}
                    ${renderEvidenceSection(d.evidence)}
                </div>`;
        }

        const typeLabel = getQuestionTypeLabel(d.question_type);
        
        card.innerHTML = `
            <div class="review-header">
                <div class="review-status ${statusClass}"><i class="fas fa-${statusIcon}"></i></div>
                <span class="question-type-badge ${['single_choice', 'multiple_choice', 'true_false'].includes(d.question_type) ? 'badge-choice' : 'badge-short'}">${typeLabel}</span>
                <span style="font-size:0.82rem;color:var(--text-muted)">第 ${idx + 1} 题</span>
                <span class="review-score ${scoreClass}">${d.score}/${d.max_score}</span>
            </div>
            <div class="review-question">${escapeHtml(d.question)}</div>
            ${answersHTML}
            ${d.explanation ? `<div class="review-explanation"><strong>解析：</strong>${escapeHtml(d.explanation)}</div>` : ''}`;

        container.appendChild(card);
    });
}

// --- Render Evidence Section with Level-based Display ---
function renderEvidenceSection(evidence) {
    if (!evidence || evidence.length === 0) {
        // 空状态处理
        return `
        <div class="review-evidence-row empty">
            <span class="review-answer-label evidence-label">📚 原文依据：</span>
            <div class="review-evidence-empty">
                <i class="fas fa-search"></i>
                <p>根据全文语义逻辑综合判定，未发现特定高相关段落</p>
            </div>
        </div>`;
    }

    // 按层级分组
    const grouped = {
        '极强相关': [],
        '强相关': [],
        '弱相关': []
    };

    evidence.forEach(ev => {
        if (grouped[ev.level]) {
            grouped[ev.level].push(ev);
        }
    });

    // 构建 HTML
    let html = `
    <div class="review-evidence-row">
        <span class="review-answer-label evidence-label">📚 原文依据：</span>
        <div class="review-evidence-list-grouped">
    `;

    // 渲染每个层级
    ['极强相关', '强相关', '弱相关'].forEach(level => {
        const items = grouped[level];
        if (items.length === 0) return;

        const levelClass = level === '极强相关' ? 'level-critical' : 
                          level === '强相关' ? 'level-strong' : 'level-weak';
        
        // 默认展开第一个“极强相关”，其他默认折叠
        const isFirstCritical = level === '极强相关' && items.length > 0;
        const isOpen = isFirstCritical ? 'open' : '';

        html += `
            <details class="evidence-level-group ${levelClass}" ${isOpen}>
                <summary class="evidence-level-summary">
                    <span class="evidence-level-title">${level}</span>
                    <span class="evidence-level-count">(${items.length}条)</span>
                    <i class="fas fa-chevron-down chevron-icon"></i>
                </summary>
                <div class="evidence-items-container">
                    ${items.map((ev, idx) => `
                        <div class="review-evidence-item">
                            <span class="evidence-badge" title="相似度 ${(ev.similarity).toFixed(1)}%">${idx + 1}</span>
                            <span class="evidence-text">${escapeHtml(ev.text)}</span>
                            <span class="evidence-score" title="余弦相似度">💯 ${(ev.similarity).toFixed(1)}%</span>
                        </div>
                    `).join('')}
                </div>
            </details>
        `;
    });

    html += `
        </div>
    </div>`;

    return html;
}

// --- Animate Number ---
function animateNumber(el, from, to, duration) {
    const start = performance.now();
    const isFloat = to % 1 !== 0;

    function tick(now) {
        const elapsed = now - start;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
        const current = from + (to - from) * eased;
        el.textContent = isFloat ? current.toFixed(1) : Math.round(current);
        if (progress < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
}

// --- Reset ---
function resetExam() {
    sessionData = null;
    document.getElementById('article-text').value = '';
    document.getElementById('char-count').textContent = '0 字';
    document.getElementById('score-ring-fill').style.strokeDashoffset = 326.73;
    goToStep(1);
    loadRecentSessions();
}

// --- Settings ---
async function openSettings() {
    try {
        fullConfig = await api('GET', '/api/config');
        
        // 加载模型列表
        const modelsList = document.getElementById('cfg-models-list');
        const extractSelect = document.getElementById('extract-model-select');
        const generateSelect = document.getElementById('generate-model-select');
        
        if (fullConfig.models) {
            // 1. 填充默认模型设置
            if (modelsList) {
                modelsList.innerHTML = '';
                fullConfig.models.forEach((m, idx) => {
                    const option = document.createElement('option');
                    option.value = idx;
                    option.textContent = `${m.name}`;
                    if (idx === fullConfig.default_model_index) option.selected = true;
                    modelsList.appendChild(option);
                });
                modelsList.onchange = (e) => updateSettingsFields(e.target.value);
            }
            
            // 2. 填充分阶段模型选择器
            const populateStageSelect = (selectEl) => {
                if (!selectEl) return;
                selectEl.innerHTML = '<option value="-1">跟随默认模型</option>';
                fullConfig.models.forEach((m, idx) => {
                    const option = document.createElement('option');
                    option.value = idx;
                    option.textContent = `${m.name}`;
                    selectEl.appendChild(option);
                });
            };
            populateStageSelect(extractSelect);
            populateStageSelect(generateSelect);
        }
        
        updateSettingsFields(fullConfig.default_model_index);
        
    } catch (e) {
        console.error('加载配置失败:', e);
    }
    document.getElementById('settings-modal').classList.add('active');
}

// 根据选中的索引，更新 UI 上的输入框内容
function updateSettingsFields(index) {
    if (!fullConfig || !fullConfig.models[index]) return;
    
    const selectedModel = fullConfig.models[index];
    document.getElementById('cfg-base-url').value = selectedModel.api_base_url || '';
    document.getElementById('cfg-model').value = selectedModel.model || '';
    // API Key 通常不回显完整字符串，但为了修改方便，清空让用户输入
    document.getElementById('cfg-api-key').value = '';
    document.getElementById('cfg-api-key').placeholder = selectedModel.api_key ? '已配置（不修改请留空）' : '输入 API Key';
    
    // 更新提示信息
    const modelHint = document.querySelector('#cfg-model + .form-hint');
    if (modelHint) {
        const thinkingText = selectedModel.thinking_enabled 
            ? ' ✅ 已启用深度思考模式' 
            : '';
        const vectorNote = !selectedModel.embedding_model
            ? '（注：向量检索将自动使用智谱）'
            : '';
        modelHint.textContent = `如 glm-4-flash、glm-4-plus、gpt-4o、deepseek-reasoner 等${thinkingText}${vectorNote}`;
    }
}

function closeSettings() {
    document.getElementById('settings-modal').classList.remove('active');
}

async function saveSettings() {
    const selectedIndex = parseInt(document.getElementById('cfg-models-list').value);
    const body = {
        model_index: selectedIndex, // 核心：告诉后端改哪一条
        api_base_url: document.getElementById('cfg-base-url').value.trim(),
        api_key: document.getElementById('cfg-api-key').value.trim(),
        model: document.getElementById('cfg-model').value.trim()
    };

    try {
        showLoading('保存中', '正在更新模型配置...');
        await api('POST', '/api/config', body);
        showToast('设置已保存并应用', 'success');
        closeSettings();
        // 重新加载配置以更新 UI
        loadApiConfig();
    } catch (e) {
        showToast('保存失败：' + e.message, 'error');
    } finally {
        hideLoading();
    }
}

function loadApiConfig() {
    // Pre-load for reference
    api('GET', '/api/config').catch(() => {});
}

// --- Sessions ---
async function loadRecentSessions() {
    try {
        const sessions = await api('GET', '/api/sessions');
        const list = document.getElementById('sessions-list');

        if (!sessions || sessions.length === 0) {
            section.style.display = 'none';
            return;
        }

        section.style.display = 'block';
        list.innerHTML = '';

        sessions.slice(0, 5).forEach(s => {
            const statusMap = {
                'in_progress': { label: '进行中', cls: 'in-progress' },
                'saved': { label: '已保存', cls: 'saved' },
                'graded': { label: '已批改', cls: 'graded' },
            };
            const st = statusMap[s.status] || { label: s.status, cls: 'saved' };

            const item = document.createElement('div');
            item.className = 'session-item';
            item.onclick = () => resumeSession(s.session_id);

            item.innerHTML = `
                <div class="session-info">
                    <h4>${escapeHtml(s.title || '未命名')}</h4>
                    <p>${s.answered || 0}/${s.total || 0} 已答 · ${formatTime(s.created_at)}</p>
                </div>
                <div class="session-meta">
                    ${s.score != null ? `<span class="session-score">${s.score}分</span>` : ''}
                    <span class="session-badge ${st.cls}">${st.label}</span>
                </div>`;

            list.appendChild(item);
        });
    } catch (e) {
        // ignore
    }
}

async function resumeSession(sessionId) {
    try {
        showLoading('正在加载', '恢复考核进度...');
        const data = await api('GET', `/api/load/${sessionId}`);

        sessionData = {
            session_id: data.session_id,
            title: data.title,
            summary: data.summary,
            questions: data.questions,
            answers: data.answers || {},
            pass_score: data.pass_score || 60,
        };

        if (data.status === 'graded') {
            // 直接使用已存储的评分结果，避免重复检索
            const gradeData = {
                score: data.score,
                passed: data.passed,
                pass_score: data.pass_score,
                details: data.details || [],
                level_analysis: {
                    basic: {
                        score: data.level_scores?.basic || 0,
                        max: data.level_max?.basic || 0,
                        rate: data.level_rates?.basic || 0
                    },
                    apply: {
                        score: data.level_scores?.apply || 0,
                        max: data.level_max?.apply || 0,
                        rate: data.level_rates?.apply || 0
                    },
                    advanced: {
                        score: data.level_scores?.advanced || 0,
                        max: data.level_max?.advanced || 0,
                        rate: data.level_rates?.advanced || 0
                    }
                }
            };
            renderResults(gradeData);
            goToStep(3);
        } else {
            renderExam();
            goToStep(2);
        }

        hideLoading();
        showToast('已恢复考核进度', 'success');
    } catch (e) {
        hideLoading();
        showToast('恢复失败: ' + e.message, 'error');
    }
}

function showSessions() {
    const modal = document.getElementById('sessions-modal');
    const list = document.getElementById('sessions-modal-list');

    api('GET', '/api/sessions').then(sessions => {
        if (!sessions || sessions.length === 0) {
            list.innerHTML = '<div class="empty-state"><i class="fas fa-inbox"></i><p>暂无历史记录</p></div>';
            return;
        }

        list.innerHTML = '';
        sessions.forEach(s => {
            const statusMap = {
                'in_progress': { label: '进行中', cls: 'in-progress' },
                'saved': { label: '已保存', cls: 'saved' },
                'graded': { label: '已批改', cls: 'graded' },
            };
            const st = statusMap[s.status] || { label: s.status, cls: 'saved' };

            const item = document.createElement('div');
            item.className = 'session-item';
            item.onclick = () => { resumeSession(s.session_id); closeSessions(); };

           item.innerHTML = `
                <div class="session-info">
                    <h4>${escapeHtml(s.title || '无标题考核')}</h4>
                    <p>${s.answered}/${s.total} 已答 · ${formatTime(s.created_at)}</p>
                </div>
                <div class="session-meta">
                    ${s.score != null ? `<span class="session-score">${s.score}分</span>` : ''}
                    <span class="session-badge ${st.cls}">${st.label}</span>
                    <button class="delete-session-btn" onclick="confirmDelete(event, '${s.session_id}')" title="删除记录">
                        <i class="fas fa-trash-alt"></i>
                    </button>
                </div>`;
            list.appendChild(item);
        });
    }).catch(() => {
        list.innerHTML = '<div class="empty-state"><p>加载失败</p></div>';
    });

    modal.classList.add('active');
}

function closeSessions() {
    document.getElementById('sessions-modal').classList.remove('active');
}

// --- Utilities ---
function getQuestionTypeLabel(type) {
    const labels = {
        'fill_blank': '填空题',
        'true_false': '判断题',
        'single_choice': '单选题',
        'multiple_choice': '多选题',
        'short_answer': '简答题',
        'case_analysis': '案例分析',
        'open_ended': '开放问答',
        'debate': '辩论题',
        'design': '设计题'
    };
    return labels[type] || type;
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function formatTime(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        const now = new Date();
        const diff = now - d;
        if (diff < 60000) return '刚刚';
        if (diff < 3600000) return Math.floor(diff / 60000) + ' 分钟前';
        if (diff < 86400000) return Math.floor(diff / 3600000) + ' 小时前';
        return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    } catch {
        return '';
    }
}

// Close modals on overlay click
document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.classList.remove('active');
    });
});

// 删除确认与执行
function confirmDelete(event, sessionId) {
    event.stopPropagation(); // 阻止冒泡，防止触发加载记录

    if (confirm('确定要永久删除这条考核记录吗？')) {
        api('DELETE', `/api/sessions/${sessionId}`)
            .then(() => {
                showToast('记录已删除', 'success');
                showSessions(); // 刷新列表
            })
            .catch(err => {
                showToast('删除失败: ' + err.message, 'error');
            });
    }
}
