-- Second Brain Neovim Plugin (Loopback API Integration)
local sb = {}

local function trim(s)
  return (s:gsub("^%s*(.-)%s*$", "%1"))
end

local function read_file_trimmed(path)
  local f = io.open(path, "r")
  if not f then
    return nil
  end
  local txt = f:read("*a")
  f:close()
  if not txt or #txt > 4096 then
    return nil
  end
  return trim(txt)
end

local function security_dir()
  local env_dir = vim.env.BRAIN_SECURITY_STATE_DIR or vim.env.SECOND_BRAIN_SECURITY_DIR
  if env_dir and env_dir ~= "" then
    return env_dir
  end
  local home = vim.loop.os_homedir()
  if vim.fn.has("win32") == 1 then
    local lad = vim.env.LOCALAPPDATA or (home .. "\\AppData\\Local")
    return lad .. "\\SecondBrain\\security"
  end
  local xdg = vim.env.XDG_STATE_HOME or (home .. "/.local/state")
  return xdg .. "/SecondBrain/security"
end

function sb.discover_endpoint()
  local sdir = security_dir()
  local sep = vim.fn.has("win32") == 1 and "\\" or "/"
  local port_file = vim.env.BRAIN_API_PORT_FILE or (sdir .. sep .. "api-port")
  local port = 8765
  local raw = read_file_trimmed(port_file)
  if raw then
    local ok, parsed = pcall(vim.json.decode, raw)
    if ok and type(parsed) == "table" and tonumber(parsed.port) then
      port = tonumber(parsed.port)
    end
  end
  local token = vim.env.BRAIN_MEMORY_API_TOKEN
    or read_file_trimmed(sdir .. sep .. "memory-api-token")
    or read_file_trimmed(sdir .. sep .. "api-token")
    or ""
  local owner = vim.env.BRAIN_OWNER_API_TOKEN
    or read_file_trimmed(sdir .. sep .. "owner-token")
    or ""
  return {
    base_url = "http://127.0.0.1:" .. tostring(port),
    token = token,
    owner_token = owner,
  }
end

local function show_float(title, lines)
  local buf = vim.api.nvim_create_buf(false, true)
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false
  vim.bo[buf].filetype = "markdown"
  local width = math.min(96, math.max(52, vim.o.columns - 12))
  local height = math.min(24, math.max(6, #lines + 2))
  vim.api.nvim_open_win(buf, true, {
    relative = "editor",
    width = width,
    height = height,
    row = math.floor((vim.o.lines - height) / 2),
    col = math.floor((vim.o.columns - width) / 2),
    style = "minimal",
    border = "rounded",
    title = " " .. title .. " ",
    title_pos = "center",
  })
end

local function escape_curl_config_value(val)
  local clean = tostring(val or ""):gsub("[\r\n]", "")
  clean = clean:gsub("\\", "\\\\"):gsub('"', '\\"')
  return clean
end

function sb.request(method, api_path, body_table, use_owner)
  local ep = sb.discover_endpoint()
  local url = ep.base_url .. api_path
  local cmd = { "curl", "-sS", "--config", "-", "-X", method, url }
  local cfg_lines = {
    'header = "Accept: application/json"',
  }
  if ep.token ~= "" then
    table.insert(cfg_lines, 'header = "Authorization: Bearer ' .. escape_curl_config_value(ep.token) .. '"')
  end
  if use_owner and ep.owner_token ~= "" then
    table.insert(cfg_lines, 'header = "X-Owner-Token: ' .. escape_curl_config_value(ep.owner_token) .. '"')
  end
  if body_table ~= nil then
    table.insert(cfg_lines, 'header = "Content-Type: application/json"')
    table.insert(cmd, "--data-raw")
    table.insert(cmd, vim.json.encode(body_table))
  end
  local config_stdin = table.concat(cfg_lines, "\n") .. "\n"
  local out = vim.fn.system(cmd, config_stdin)
  if vim.v.shell_error ~= 0 then
    return nil, "Failed to reach Second Brain API at " .. ep.base_url
  end
  local ok, decoded = pcall(vim.json.decode, out)
  if not ok then
    return nil, "Malformed JSON response from Second Brain API"
  end
  return decoded, nil
end

function sb.recall(query)
  if not query or trim(query) == "" then
    query = vim.fn.input("Second Brain Recall Query: ")
  end
  if trim(query) == "" then
    return
  end
  local data, err = sb.request("POST", "/v1/recall", { query = query, n_results = 8 }, false)
  if not data then
    vim.notify(err or "Recall failed", vim.log.levels.ERROR)
    return
  end
  local lines = { "# Second Brain Verified Recall: " .. query, "" }
  local results = data.direct_results or data.results or {}
  if #results == 0 then
    table.insert(lines, "_No owner-approved verified atoms matched this query._")
  else
    for i, item in ipairs(results) do
      table.insert(lines, string.format("## [V%d] %s", i, tostring(item.filename or "atom.md")))
      table.insert(lines, tostring(item.claim or item.content or ""))
      table.insert(lines, "")
    end
  end
  show_float("Second Brain Recall", lines)
end

function sb.remember(text)
  local content = text and trim(text) or ""
  if content == "" then
    content = vim.fn.input("Remember note (staged in 0-raw/): ")
  end
  if content == "" then
    return
  end
  local data, err = sb.request("POST", "/v1/remember", { content = content, source = "neovim" }, false)
  if not data then
    vim.notify(err or "Remember failed", vim.log.levels.ERROR)
    return
  end
  vim.notify("Second Brain remembered: " .. tostring(data.filename or data.status or "saved"), vim.log.levels.INFO)
end

function sb.plan(goal)
  if not goal or trim(goal) == "" then
    goal = vim.fn.input("Executive Plan Goal: ")
  end
  if trim(goal) == "" then
    return
  end
  local data, err = sb.request("POST", "/v1/plan", { goal = goal, mode = "auto" }, false)
  if not data then
    vim.notify(err or "Plan failed", vim.log.levels.ERROR)
    return
  end
  local lines = { "# Executive Plan: " .. goal, "", tostring(data.summary or ""), "" }
  for i, step in ipairs(data.steps or {}) do
    table.insert(lines, string.format("%d. %s", i, tostring(step.instruction or "")))
  end
  show_float("Second Brain Plan", lines)
end

function sb.setup()
  vim.api.nvim_create_user_command("SecondBrainRecall", function(opts)
    sb.recall(opts.args)
  end, { nargs = "*" })
  vim.api.nvim_create_user_command("SecondBrainRemember", function(opts)
    sb.remember(opts.args)
  end, { nargs = "*", range = true })
  vim.api.nvim_create_user_command("SecondBrainPlan", function(opts)
    sb.plan(opts.args)
  end, { nargs = "*" })
end

return sb
