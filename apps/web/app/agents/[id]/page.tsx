import { AgentDetailRouteView } from '@/components/route-views'
export default async function Page({ params }: { params: Promise<{ id: string }> }) { return <AgentDetailRouteView id={(await params).id} /> }
