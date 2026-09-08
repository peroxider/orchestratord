import { SquadDetailRouteView } from '@/components/route-views'
export default async function Page({ params }: { params: Promise<{ id: string }> }) { return <SquadDetailRouteView id={(await params).id} /> }
