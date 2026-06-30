// secure context(HTTPS/localhost)가 아니면 navigator.clipboard가 undefined이므로
// execCommand 폴백으로 처리한다. (예: http://192.168.x.x:8000 로 접속하는 경우)
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // 폴백으로 진행
    }
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  document.execCommand('copy')
  document.body.removeChild(ta)
}
