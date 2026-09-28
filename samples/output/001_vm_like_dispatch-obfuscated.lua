-- Deobfuscated by deobf (dynamic trace)
-- source: 001_vm_like_dispatch-obfuscated.lua
-- NOTE: reconstructed from observed behaviour; branches that were not taken
--       during the trace are missing and conditions are only noted in comments.
-- run status: finished
-- 44 statements recorded in 0.71s

-- [deobf] folded 6 repeated calls into 1 helper functions and 0 unrolled runs into loops
-- [deobf] removed 26 lines of the obfuscator's environment/anti-tamper probes
local function onAttributeChanged(value)
	local connection = value.AttributeChanged:Connect(function(attribute)
	end)

	connection:Disconnect()
end

onAttributeChanged(game)
onAttributeChanged(workspace)
local Folder = Instance.new("Folder")
onAttributeChanged(Folder)
Folder:GetChildren()
Folder:Destroy()
local Folder2 = Instance.new("Folder", Folder)
onAttributeChanged(Folder2)
Folder2.Name = "1392044856"
Folder:WaitForChild("1392044856")
Folder:Destroy()
Folder2:Destroy()
local HttpService = game:GetService("HttpService")
onAttributeChanged(HttpService)
local RunService = game:GetService("RunService")
onAttributeChanged(RunService)
