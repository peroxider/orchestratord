import { SessionDetailRouteView } from '@/components/route-views'
export default async function Page({ params }: { params: Promise<{ id: string }> }) { const { id } = await params; return <SessionDetailRouteView id={id} /> }
