// Floating chat widget (see api_chat() in app.py). Stateless backend - this
// script owns the running conversation history and resends it each turn.
(function () {
    var toggle = document.getElementById('chatbot-toggle');
    var panel = document.getElementById('chatbot-panel');
    var messagesEl = document.getElementById('chatbot-messages');
    var form = document.getElementById('chatbot-form');
    var input = document.getElementById('chatbot-input');
    if (!toggle || !panel || !form || !input) return;

    var history = [];

    toggle.addEventListener('click', function () {
        var opening = panel.hasAttribute('hidden');
        if (opening) {
            panel.removeAttribute('hidden');
            input.focus();
            if (!messagesEl.childElementCount) {
                addMessage('assistant', 'Здравейте! С какво мога да помогна?');
            }
        } else {
            panel.setAttribute('hidden', '');
        }
    });

    function addMessage(role, text) {
        var el = document.createElement('div');
        el.className = 'chatbot-msg chatbot-msg-' + role;
        el.textContent = text;
        messagesEl.appendChild(el);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        return el;
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        var text = input.value.trim();
        if (!text) return;
        input.value = '';
        input.disabled = true;
        addMessage('user', text);
        var typingEl = addMessage('assistant', 'Пише...');

        fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.CSRF_TOKEN },
            body: JSON.stringify({ message: text, history: history }),
        })
            .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
            .then(function (result) {
                typingEl.remove();
                if (result.ok && result.data.status === 'ok') {
                    addMessage('assistant', result.data.reply);
                    history.push({ role: 'user', content: text });
                    history.push({ role: 'assistant', content: result.data.reply });
                } else {
                    addMessage('assistant', (result.data && result.data.message) || 'Възникна грешка. Опитайте отново.');
                }
            })
            .catch(function () {
                typingEl.remove();
                addMessage('assistant', 'Възникна грешка с връзката. Опитайте отново.');
            })
            .finally(function () {
                input.disabled = false;
                input.focus();
            });
    });
})();
