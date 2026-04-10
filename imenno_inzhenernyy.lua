local function input_number(prompt)
    io.write(prompt)
    local input = io.read()
    local number = tonumber(input)
    if not number then
        print("Ошибка: введите число")
        return input_number(prompt)
    end
    return number
end

local function input_operation()
    print("Выберите операцию:")
    print("1. Сложение (+)")
    print("2. Вычитание (-)")
    print("3. Умножение (*)")
    print("4. Деление (/)")
    print("5. Возведение в степень (^)")
    print("6. Синус (sin)")
    print("7. Косинус (cos)")
    print("8. Тангенс (tan)")
    print("9. Логарифм (log)")
    print("10. Натуральный логарифм (ln)")
    print("11. Квадратный корень (√)")
    print("12. Факториал (!)")
    print("13. Обратное значение (1/x)")
    print("14. Пи (π)")
    print("15. Е (e)")
    io.write("Введите номер операции: ")
    local choice = io.read()
    return tonumber(choice)
end

local function factorial(n)
    if n < 0 then
        return nil
    end
    if n == 0 or n == 1 then
        return 1
    end
    local result = 1
    for _ = 2, n do
        result = result * _
    end
    return result
end

local function calculate()
    local operation = input_operation()
    
    if operation >= 1 and operation <= 4 then
        local a = input_number("Введите первое число: ")
        local b = input_number("Введите второе число: ")
        
        if operation == 1 then
            print("Результат: " .. a + b)
        elseif operation == 2 then
            print("Результат: " .. a - b)
        elseif operation == 3 then
            print("Результат: " .. a * b)
        elseif operation == 4 then
            if b == 0 then
                print("Ошибка: деление на ноль")
            else
                print("Результат: " .. a / b)
            end
        elseif operation == 5 then
            print("Результат: " .. a ^ b)
        end
        
    elseif operation >= 6 and operation <= 13 then
        local x = input_number("Введите число: ")
        
        if operation == 6 then
            print("Результат: " .. math.sin(x))
        elseif operation == 7 then
            print("Результат: " .. math.cos(x))
        elseif operation == 8 then
            print("Результат: " .. math.tan(x))
        elseif operation == 9 then
            if x <= 0 then
                print("Ошибка: логарифм от не положительного числа")
            else
                print("Результат: " .. math.log10(x))
            end
        elseif operation == 10 then
            if x <= 0 then
                print("Ошибка: натуральный логарифм от не положительного числа")
            else
                print("Результат: " .. math.log(x))
            end
        elseif operation == 11 then
            if x < 0 then
                print("Ошибка: квадратный корень от отрицательного числа")
            else
                print("Результат: " .. math.sqrt(x))
            end
        elseif operation == 12 then
            if x < 0 or x ~= math.floor(x) then
                print("Ошибка: факториал от отрицательного или не целого числа")
            else
                local fact = factorial(x)
                if fact then
                    print("Результат: " .. fact)
                else
                    print("Ошибка: факториал слишком большой")
                end
            end
        elseif operation == 13 then
            if x == 0 then
                print("Ошибка: деление на ноль")
            else
                print("Результат: " .. 1 / x)
            end
        end
        
    elseif operation == 14 then
        print("Результат: " .. math.pi)
        
    elseif operation == 15 then
        print("Результат: " .. math.exp(1))
        
    else
        print("Ошибка: неверный номер операции")
    end
end

print("Инженерный калькулятор")
print("======================")

while true do
    calculate()
    io.write("\nХотите продолжить? (y/n): ")
    local answer = io.read()
    if answer ~= "y" and answer ~= "Y" then
        break
    end
    print()
end

print("Спасибо за использование калькулятора!")

-- Очистка переменных после REST запроса
local function clear_rest_variables(data)
    for _, result in ipairs(data.wf.vars.RESTbody.result) do
        result.ID = nil
        result.ENTITY_ID = nil
        result.CALL = nil
    end
end

-- Пример использования функции очистки переменных
local rest_data = {
    wf = {
        vars = {
            RESTbody = {
                result = {
                    {ID = 123, ENTITY_ID = 456, CALL = "example_call_1", OTHER_KEY_1 = "value1", OTHER_KEY_2 = "value2"},
                    {ID = 789, ENTITY_ID = 101, CALL = "example_call_2", EXTRA_KEY_1 = "value3", EXTRA_KEY_2 = "value4"}
                }
            }
        }
    }
}

clear_rest_variables(rest_data)
