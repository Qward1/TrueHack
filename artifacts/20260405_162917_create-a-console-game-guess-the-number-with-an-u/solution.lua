local run_mode = ...

local M = {}

function M.main()
    print('Welcome to Guess the Number with an Upgrade Shop!')
    local number = math.random(1, 100)
    local attempts = 0
    while true do
        io.write('Enter your guess: ')
        local guess = tonumber(io.read())
        if not guess then
            print('Invalid input. Please enter a number.')
            goto continue
        end
        attempts = attempts + 1
        if guess < number then
            print('Too low!')
        elseif guess > number then
            print('Too high!')
        else
            print('Congratulations! You guessed the number in ' .. attempts .. ' attempts.')
            break
        end
        ::continue::
    end
end

function M.open_shop()
    print('Welcome to the Upgrade Shop!')
    -- Add shop logic here
end

function M.buy_item(item)
    print('You bought an item: ' .. item)
end

function M.sell_item(item)
    print('You sold an item: ' .. item)
end

function M.player()
    return {
        money = 100,
        level = 1,
        experience = 0 -- Added player experience stat
    }
end

function M.apply_level_up(level)
    print('Level up! New level: ' .. level)
end

if run_mode ~= '__test__' then
    M.main()
end

return M
