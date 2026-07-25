import { redirect } from 'next/navigation'

// Recording moved inside the producer app shell (it's now a normal `record`
// view alongside Settings — see app/page.tsx). This route is kept only so old
// links/bookmarks to /record still land on the in-shell recording view.
export default function RecordPage() {
  redirect('/?view=record')
}
