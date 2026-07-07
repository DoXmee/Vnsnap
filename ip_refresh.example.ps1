# Copy file này thành ip_refresh.ps1 nếu muốn TikTok TTS tự đổi IP khi bị 429/blocked.
# Tool sẽ tự chạy file ip_refresh.ps1 này, chờ xong rồi retry.
#
# Lưu ý:
# - Mạng cáp/fiber thường không chắc đổi IP chỉ bằng lệnh trên máy.
# - Ổn nhất là VPN CLI, 4G/5G router API, hoặc modem/router có endpoint reconnect.
# - Không lưu mật khẩu thật vào file nếu máy dùng chung.
#
# Ví dụ 1: gọi script/router API riêng của bạn:
# Invoke-WebRequest -UseBasicParsing -Uri "http://192.168.1.1/reconnect" | Out-Null
#
# Ví dụ 2: reconnect VPN CLI giả định:
# & "C:\Program Files\YourVPN\vpn.exe" disconnect
# Start-Sleep -Seconds 5
# & "C:\Program Files\YourVPN\vpn.exe" connect --random
#
# Ví dụ 3: nếu dùng điện thoại Android qua adb, tự bật/tắt airplane mode có thể cần quyền:
# adb shell cmd connectivity airplane-mode enable
# Start-Sleep -Seconds 5
# adb shell cmd connectivity airplane-mode disable
#
Write-Host "Chưa cấu hình lệnh đổi IP thật. Hãy sửa ip_refresh.ps1 trước khi dùng."
exit 1
