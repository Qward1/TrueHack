local run_mode = ...

-- Define the main module table
local M = {}

-- Player data structure
M.player = {
    health = 100,
    energy = 100,
    hunger = 50,
    gold = 100,
    experience = 0,
    level = 1,
    inventory = {}
}

-- Inventory functions
function M.add_item(item)
    table.insert(M.player.inventory, item)
end

function M.use_item(item_name)
    for i, item in ipairs(M.player.inventory) do
        if item.name == item_name then
            -- Implement item usage logic here
            print("Using " .. item_name)
            table.remove(M.player.inventory, i)
            return true
        end
    end
    print("Item not found")
    return false
end

-- Shop functions
M.shop_items = {
    { name = "Potion", price = 10 },
    { name = "Sword", price = 50 },
    { name = "Shield", price = 30 }
}

function M.open_shop()
    print("Welcome to the shop!")
    for i, item in ipairs(M.shop_items) do
        print(i .. ". " .. item.name .. " - $" .. item.price)
    end
end

function M.buy_item(item_index)
    if item_index > #M.shop_items then
        print("Invalid item index")
        return
    end
    local item = M.shop_items[item_index]
    if M.player.gold >= item.price then
        M.add_item(item)
        M.player.gold = M.player.gold - item.price
        print("Bought " .. item.name)
    else
        print("Not enough gold")
    end
end

function M.sell_item(item_name)
    for i, item in ipairs(M.player.inventory) do
        if item.name == item_name then
            local price = math.floor(item.price * 0.8) -- Sell at 20% discount
            M.player.gold = M.player.gold + price
            table.remove(M.player.inventory, i)
            print("Sold " .. item_name .. " for $" .. price)
            return true
        end
    end
    print("Item not found")
    return false
end

-- Battle functions
function M.run_battle(enemy)
    local player = M.player
    while player.health > 0 and enemy.health > 0 do
        print("Player health: " .. player.health .. ", Enemy health: " .. enemy.health)
        io.write("Choose an action (attack/defend/run): ")
        local action = io.read()
        if action == "attack" then
            M.attack_enemy(enemy)
        elseif action == "defend" then
            M.defend_turn()
        elseif action == "run" then
            print("You ran away!")
            return
        else
            print("Invalid action")
        end
    end
    if player.health <= 0 then
        print("You were defeated!")
    else
        print("You won the battle!")
        player.experience = player.experience + enemy.experience_reward
        M.apply_level_up()
    end
end

function M.attack_enemy(enemy)
    local damage = math.random(1, 20)
    enemy.health = enemy.health - damage
    print("You dealt " .. damage .. " damage to the enemy")
end

function M.defend_turn()
    -- Implement defend logic here
    print("Defending...")
end

-- Quest functions
M.quests = {
    { name = "Kill 5 goblins", completed = false },
    { name = "Find a rare potion", completed = false },
    { name = "Collect 10 gold coins", completed = false }
}

function M.update_quests()
    for i, quest in ipairs(M.quests) do
        if not quest.completed then
            print("Quest: " .. quest.name)
        end
    end
end

-- Location data and functions
M.locations = {
    { name = "Forest", event = "Battle" },
    { name = "Cave", event = "Find Resource" },
    { name = "Village", event = "Trade" },
    { name = "Ruins", event = "Random Event" },
    { name = "Campsite", event = "Rest" }
}

function M.visit_location(location_name)
    for _, location in ipairs(M.locations) do
        if location.name == location_name then
            print("You are now in the " .. location_name)
            local event = location.event
            if event == "Battle" then
                -- Implement battle logic here
                local enemy = { health = 50, experience_reward = 10 }
                M.run_battle(enemy)
            elseif event == "Find Resource" then
                -- Implement find resource logic here
                print("You found some resources!")
            elseif event == "Trade" then
                M.open_shop()
            elseif event == "Random Event" then
                -- Implement random event logic here
                local events = {"Battle", "Find Resource", "Trade"}
                local random_event = events[math.random(1, #events)]
                if random_event == "Battle" then
                    local enemy = { health = 50, experience_reward = 10 }
                    M.run_battle(enemy)
                elseif random_event == "Find Resource" then
                    print("You found some resources!")
                elseif random_event == "Trade" then
                    M.open_shop()
                end
            elseif event == "Rest" then
                -- Implement rest logic here
                print("You rested and recovered.")
                M.player.health = 100
                M.player.energy = 100
                M.player.hunger = 50
            end
            return
        end
    end
    print("Location not found")
end

function M.generate_location_event(location_name)
    -- Implement event generation logic here
    local location = M.locations[location_name]
    if location.event == "Battle" then
        -- Implement battle logic here
        local enemy = { health = 50, experience_reward = 10 }
        M.run_battle(enemy)
    elseif location.event == "Find Resource" then
        -- Implement find resource logic here
        print("You found some resources!")
    elseif location.event == "Trade" then
        M.open_shop()
    elseif location.event == "Random Event" then
        -- Implement random event logic here
        local events = {"Battle", "Find Resource", "Trade"}
        local random_event = events[math.random(1, #events)]
        if random_event == "Battle" then
            local enemy = { health = 50, experience_reward = 10 }
            M.run_battle(enemy)
        elseif random_event == "Find Resource" then
            print("You found some resources!")
        elseif random_event == "Trade" then
            M.open_shop()
        end
    elseif location.event == "Rest" then
        -- Implement rest logic here
        print("You rested and recovered.")
        M.player.health = 100
        M.player.energy = 100
        M.player.hunger = 50
    end
end

-- Save and load game functions
function M.save_game()
    local file = io.open("savegame.txt", "w")
    if file then
        file:write(json.encode(M.player))
        file:close()
        print("Game saved successfully!")
    else
        print("Failed to save game.")
    end
end

function M.load_game()
    local file = io.open("savegame.txt", "r")
    if file then
        local data = file:read("*all")
        file:close()
        M.player = json.decode(data)
        print("Game loaded successfully!")
    else
        print("No save game found.")
    end
end

-- Level up function
function M.apply_level_up()
    while M.player.experience >= 100 * M.player.level do
        M.player.level = M.player.level + 1
        M.player.health = M.player.health + 20
        M.player.energy = M.player.energy + 20
        M.player.hunger = M.player.hunger + 10
        print("Level up! New level: " .. M.player.level)
    end
end

-- Main function to start the game
function M.main()
    if run_mode ~= '__test__' then
        print("Welcome to the RPG Game!")
        while true do
            print("1. Visit Forest")
            print("2. Visit Cave")
            print("3. Visit Village")
            print("4. Visit Ruins")
            print("5. Visit Campsite")
            print("6. Update Quests")
            print("7. Save Game")
            print("8. Load Game")
            print("9. Exit")
            io.write("Choose an option: ")
            local choice = io.read()
            if choice == "1" then
                M.visit_location("Forest")
            elseif choice == "2" then
                M.visit_location("Cave")
            elseif choice == "3" then
                M.visit_location("Village")
            elseif choice == "4" then
                M.visit_location("Ruins")
            elseif choice == "5" then
                M.visit_location("Campsite")
            elseif choice == "6" then
                M.update_quests()
            elseif choice == "7" then
                M.save_game()
            elseif choice == "8" then
                M.load_game()
            elseif choice == "9" then
                print("Exiting game...")
                break
            else
                print("Invalid option")
            end
        end
    end
end

-- Return the module table

if run_mode ~= '__test__' then
    M.main()
end

return M
