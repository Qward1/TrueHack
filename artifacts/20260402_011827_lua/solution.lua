local M = {}

function M.request_input()
    print('Введите данные:')
    local user_input = io.read()
    return user_input
end

function M.display_output(output)
    print('Вы ввели: ' .. output)
end

function M.main()
    local input = M.request_input()
    M.display_output(input)
end

local run_mode = ...
if run_mode ~= '__test__' then
    M.main()
end

return M