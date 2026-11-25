if vim.g.loaded_second_brain_nvim then
  return
end
vim.g.loaded_second_brain_nvim = true
require("second_brain").setup()
