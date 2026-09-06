// Agentic Workday OS v2 Frontend Controller with Semester Scope

let currentTerm = "26S1";

document.addEventListener("DOMContentLoaded", () => {
  fetchTerms();
  fetchStatus();
  refreshAll();

  // Periodic poll for background sync status
  setInterval(fetchStatus, 4000);
});

function refreshAll() {
  fetchSchedule();
  fetchTasks();
  fetchCourses();
  fetchAnnouncements();
}

function changeTerm(newTerm) {
  currentTerm = newTerm;
  showToast(`Switched view to Semester: ${newTerm}`);
  refreshAll();
}

function showToast(msg) {
  const toast = document.getElementById("toast");
  toast.innerText = msg;
  toast.style.display = "block";
  setTimeout(() => {
    toast.style.display = "none";
  }, 4000);
}

async function fetchTerms() {
  try {
    const res = await fetch("/api/terms");
    const terms = await res.json();
    const select = document.getElementById("term-select");
    if (!select) return;

    let html = `<option value="26S1" ${currentTerm === '26S1' ? 'selected' : ''}>26S1 (Current Active)</option>`;
    for (const t of terms) {
      if (t !== "26S1" && t !== "General") {
        html += `<option value="${t}" ${currentTerm === t ? 'selected' : ''}>${t}</option>`;
      }
    }
    html += `<option value="All" ${currentTerm === 'All' ? 'selected' : ''}>All Semesters</option>`;
    select.innerHTML = html;
  } catch (err) {
    console.error("Failed to load terms:", err);
  }
}

async function fetchStatus() {
  try {
    const res = await fetch(`/api/status?term=${currentTerm}`);
    const data = await res.json();

    const bedrockBadge = document.getElementById("bedrock-badge");
    if (data.bedrock && data.bedrock.ready) {
      bedrockBadge.className = "status-pill status-ready";
      bedrockBadge.innerHTML = `<span class="dot"></span> Bedrock: Online (${data.bedrock.region})`;
    } else {
      bedrockBadge.className = "status-pill status-error";
      bedrockBadge.innerHTML = `<span class="dot"></span> Bedrock: Offline`;
    }

    const ntuBadge = document.getElementById("ntulearn-badge");
    if (data.ntulearn_authenticated) {
      ntuBadge.className = "status-pill status-ready";
      ntuBadge.innerHTML = `<span class="dot"></span> NTULearn: Active`;
    } else {
      ntuBadge.className = "status-pill status-loading";
      ntuBadge.innerHTML = `<span class="dot"></span> NTULearn: Need Login`;
      ntuBadge.onclick = triggerLogin;
      ntuBadge.style.cursor = "pointer";
    }

    if (data.sync_job && data.sync_job.status === "running") {
      document.getElementById("btn-sync").innerHTML = `<span class="btn-icon">⏳</span> Syncing...`;
    } else {
      document.getElementById("btn-sync").innerHTML = `<span class="btn-icon">🔄</span> Sync Blackboard`;
    }
  } catch (err) {
    console.error("Failed to fetch status:", err);
  }
}

async function triggerSync() {
  showToast(`Syncing Blackboard for ${currentTerm}...`);
  try {
    const res = await fetch(`/api/sync?term=${currentTerm}`, { method: "POST" });
    const data = await res.json();
    if (data.message) {
      showToast(data.message);
      setTimeout(refreshAll, 3000);
    }
  } catch (err) {
    showToast("Error triggering sync: " + err);
  }
}

async function triggerLogin() {
  showToast("Opening browser for NTU SSO & 2FA login...");
  try {
    await fetch("/api/login/ntulearn", { method: "POST" });
  } catch (err) {
    showToast("Error opening login: " + err);
  }
}

async function fetchSchedule() {
  try {
    const res = await fetch(`/api/schedule?term=${currentTerm}`);
    const schedule = await res.json();
    const container = document.getElementById("schedule-timeline");
    document.getElementById("schedule-counter").innerText = `${schedule.length} Blocks`;

    if (!schedule || schedule.length === 0) {
      container.innerHTML = `<div class="empty-state">No scheduled blocks for ${currentTerm}. Add tasks to populate your day!</div>`;
      return;
    }

    container.innerHTML = schedule.map(item => `
      <div class="timeline-card">
        <div>
          <div class="timeline-time">${item.start} — ${item.end} (${item.duration_mins}m)</div>
          <div class="timeline-title">${escapeHtml(item.title)}</div>
        </div>
        <span class="count-tag">${item.course_code || 'General'}</span>
      </div>
    `).join("");
  } catch (err) {
    console.error("Failed to fetch schedule:", err);
  }
}

async function fetchTasks() {
  try {
    const res = await fetch(`/api/tasks?term=${currentTerm}`);
    const tasks = await res.json();
    const container = document.getElementById("tasks-list");

    if (!tasks || tasks.length === 0) {
      container.innerHTML = `<div class="empty-state">No active tasks for ${currentTerm}.</div>`;
      return;
    }

    container.innerHTML = tasks.map(task => {
      const isCompleted = task.status === "completed";
      const priorityClass = task.priority_score >= 8 ? "priority-high" : "priority-med";
      return `
        <div class="task-item ${isCompleted ? 'completed' : ''}">
          <div style="display: flex; align-items: center;">
            <button class="task-check-btn" onclick="toggleTask('${task.id}')">${isCompleted ? '✓' : ''}</button>
            <div>
              <div style="font-size: 0.85rem; font-weight: 500;">${escapeHtml(task.title)}</div>
              <div style="font-size: 0.72rem; color: var(--text-muted);">${task.course_code || 'General'} • Due: ${task.due_date || 'None'}</div>
            </div>
          </div>
          <span class="priority-badge ${priorityClass}">★ ${task.priority_score}</span>
        </div>
      `;
    }).join("");
  } catch (err) {
    console.error("Failed to fetch tasks:", err);
  }
}

async function toggleTask(taskId) {
  try {
    await fetch(`/api/tasks/${taskId}/toggle`, { method: "POST" });
    fetchTasks();
    fetchSchedule();
  } catch (err) {
    showToast("Failed to toggle task: " + err);
  }
}

async function fetchCourses() {
  try {
    const res = await fetch(`/api/courses?term=${currentTerm}`);
    const courses = await res.json();
    const container = document.getElementById("courses-list");
    document.getElementById("courses-count").innerText = `${courses.length} Modules`;

    if (!courses || courses.length === 0) {
      container.innerHTML = `<div class="empty-state">No modules found for ${currentTerm}. Click Sync Blackboard!</div>`;
      return;
    }

    container.innerHTML = courses.map(c => `
      <div class="course-card">
        <div class="course-code">${escapeHtml(c.course_code)} <span class="badge-version">${c.term || 'General'}</span></div>
        <div class="course-title">${escapeHtml(c.title)}</div>
      </div>
    `).join("");
  } catch (err) {
    console.error("Failed to fetch courses:", err);
  }
}

async function fetchAnnouncements() {
  try {
    const res = await fetch(`/api/announcements?term=${currentTerm}`);
    const items = await res.json();
    const container = document.getElementById("announcements-feed");

    if (!items || items.length === 0) {
      container.innerHTML = `<div class="empty-state">No announcements recorded for ${currentTerm}.</div>`;
      return;
    }

    container.innerHTML = items.map(a => `
      <div class="announcement-card">
        <div style="display: flex; justify-content: space-between; margin-bottom: 2px;">
          <span class="course-code">${escapeHtml(a.course_code || 'Module')}</span>
          <span class="ann-date">${escapeHtml((a.posted_at || '').substring(0, 10))}</span>
        </div>
        <div style="font-size: 0.82rem; font-weight: 600;">${escapeHtml(a.title)}</div>
        <p style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">${escapeHtml((a.body || '').substring(0, 140))}...</p>
      </div>
    `).join("");
  } catch (err) {
    console.error("Failed to fetch announcements:", err);
  }
}

async function handleChatSubmit(e) {
  e.preventDefault();
  const input = document.getElementById("chat-input");
  const message = input.value.trim();
  if (!message) return;

  input.value = "";
  appendChatMessage("user", message, "You");

  const sendBtn = document.getElementById("btn-send");
  sendBtn.disabled = true;
  sendBtn.innerText = "...";

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, term: currentTerm }),
    });
    const data = await res.json();
    appendChatMessage("assistant", data.reply, data.agent || "Lead Orchestrator");
  } catch (err) {
    appendChatMessage("assistant", "⚠️ Error reaching multi-agent backend: " + err, "System");
  } finally {
    sendBtn.disabled = false;
    sendBtn.innerText = "Send";
  }
}

function sendPrompt(promptText) {
  document.getElementById("chat-input").value = promptText;
  document.getElementById("chat-form").dispatchEvent(new Event("submit"));
}

function appendChatMessage(role, content, senderName) {
  const container = document.getElementById("chat-messages");
  const bubble = document.createElement("div");
  bubble.className = `chat-bubble ${role}`;
  bubble.innerHTML = `
    <div class="sender-tag">${escapeHtml(senderName)}</div>
    <div style="white-space: pre-wrap;">${escapeHtml(content)}</div>
  `;
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
}

function openAddTaskModal() {
  const title = prompt(`Enter new task for ${currentTerm}:`);
  if (title) {
    fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, term: currentTerm, priority_score: 7.0 }),
    }).then(() => {
      fetchTasks();
      fetchSchedule();
    });
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
