import { RuntimeDetailRouteView } from '@/components/route-views'
export default async function Page({ params }: { params: Promise<{ id: string }> }) { return <RuntimeDetailRouteView id={(await params).id} /> }
